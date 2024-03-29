###############################################################################
# Content Selection and Planning Module                                       #
###############################################################################

import torch
import torch.nn as nn
import torch.nn.functional as F
import logging
from torch import optim
from random import random
from torch.utils.data import DataLoader
from ignite.engine import Engine, Events
from ignite.handlers import ModelCheckpoint
from util.planner import load_planner_data
from util.constants import BOS_WORD, EOS_WORD, PAD_WORD
from os import path, makedirs
from util.constants import device
from util.helper_funcs import to_device
from util.metrics import BleuScore


class RecordEncoder(nn.Module):
    def __init__(self, input_size, hidden_size=600):
        super(RecordEncoder, self).__init__()
        self.encoded = None
        self.hidden_size = hidden_size

        self.embedding = nn.Embedding(input_size, hidden_size)
        self.relu_mlp = nn.Sequential(
            nn.Linear(4 * hidden_size, hidden_size),
            nn.LeakyReLU())
        self.linear = nn.Linear(hidden_size, hidden_size, bias=False)
        self.sigmoid_mlp = nn.Sequential(
            nn.Linear(2 * hidden_size, hidden_size, bias=False),
            nn.Sigmoid())

    def forward(self, records):
        """
        Content selection gate. Determines importance vis-a-vis other records.
        """
        # size = (Batch, Records, 4, hidden_size)
        embedded = self.embedding(records)
        # size = (Batch, Records, 4 * hidden_size)
        emb_cat = embedded.view(embedded.size(0), embedded.size(1), -1)
        # size = (Batch, Records, hidden_size)
        emb_relu = self.relu_mlp(emb_cat)

        # compute attention
        # size = (Batch, hidden_size, Records)
        emb_lin = self.linear(emb_relu).transpose(1, 2)
        # size = (Batch, Records, Records)
        logits = torch.bmm(emb_relu, emb_lin)
        attention = F.softmax(logits, dim=2)

        # apply attention
        # size = (Batch, Records, hidden_size)
        emb_att = torch.bmm(attention, emb_relu)
        emb_gate = self.sigmoid_mlp(torch.cat((emb_relu, emb_att), 2))
        self.encoded = emb_gate * emb_relu

        return self.encoded

    def get_encodings(self, index):
        """
        Get the record representations at the specified indices.
        """
        # size = (batch_size, indices) => size = (batch_size, indices, hidden_size)
        index = index.unsqueeze(2).repeat(1, 1, self.hidden_size)
        records = self.encoded.gather(1, index)

        return records


class ContentPlanner(nn.Module):
    def __init__(self, input_size, hidden_size=600):
        super(ContentPlanner, self).__init__()
        self.hidden_size = hidden_size
        self.selected_content = None

        self.record_encoder = RecordEncoder(input_size, hidden_size)
        self.rnn = nn.LSTM(hidden_size, hidden_size, batch_first=True)
        self.linear = nn.Linear(hidden_size, hidden_size, bias=False)

    def forward(self, index, hidden, cell):
        """
        Content Planning. Uses attention to create pointers to the input records.
        """
        # size = (batch_size) => size = (batch_size, 1, hidden_size)
        index = index.view(-1, 1, 1).repeat(1, 1, self.hidden_size)
        input_ = self.selected_content.gather(1, index)

        # size = (batch_size, 1, hidden_size)
        output, (hidden, cell) = self.rnn(input_, (hidden, cell))
        # size = (batch_size, hidden_size, records)
        content_tp = self.linear(self.selected_content).transpose(1, 2)
        # size = (batch_size, 1, records)
        logits = torch.bmm(output, content_tp)
        # size = (batch_size, records)
        attention = F.log_softmax(logits, dim=2).squeeze(1)

        return attention, hidden, cell

    def init_hidden(self, records):
        """
        Compute the initial hidden state and cell state of the Content Planning LSTM.
        """
        self.selected_content = self.record_encoder(records)
        # transpose first and second dim, because LSTM expects seq_len first
        hidden = torch.mean(self.selected_content, dim=1, keepdim=True).transpose(0, 1)
        cell = torch.zeros_like(hidden)

        return hidden, cell

###############################################################################
# Training & Evaluation functions                                             #
###############################################################################


def train_planner(extractor, epochs=25, learning_rate=0.15, acc_val_init=0.1,
                  clip=7, teacher_forcing_ratio=1.0, log_interval=100):
    data = load_planner_data("train", extractor)
    loader = DataLoader(data, shuffle=True, pin_memory=torch.cuda.is_available())  # online learning

    content_planner = ContentPlanner(len(data.idx2word)).to(device)
    optimizer = optim.Adagrad(content_planner.parameters(), lr=learning_rate, initial_accumulator_value=acc_val_init)

    logging.info("Training a new Content Planner...")

    def _update(engine, batch):
        """
        Update function for the Conent Selection & Planning Module.
        Right now only online learning is supported
        """
        content_planner.train()
        optimizer.zero_grad()
        use_teacher_forcing = True if random() < teacher_forcing_ratio else False

        records, content_plan = to_device(batch)
        hidden, cell = content_planner.init_hidden(records)
        content_plan_iterator = iter(content_plan.t())
        input_index = next(content_plan_iterator)
        loss = 0
        len_sequence = 0

        for record_pointer in content_plan_iterator:
            if record_pointer.cpu() == data.vocab[PAD_WORD]:
                break
            output, hidden, cell = content_planner(input_index, hidden, cell)
            loss += F.nll_loss(output, record_pointer)
            len_sequence += 1
            if use_teacher_forcing:
                input_index = record_pointer
            else:
                input_index = output.argmax(dim=1).detach()

        loss.backward()
        nn.utils.clip_grad_norm_(content_planner.parameters(), clip)
        optimizer.step()
        return loss.item() / len_sequence  # normalize loss for logging

    trainer = Engine(_update)
    # save the model every 4 epochs
    handler = ModelCheckpoint("data/model_cache", "planner", save_interval=4,
                              require_empty=False, save_as_state_dict=True)
    trainer.add_event_handler(Events.EPOCH_COMPLETED, handler, {"planner": content_planner})

    @trainer.on(Events.ITERATION_COMPLETED)
    def _log_training_loss(engine):
        iteration = engine.state.iteration
        batch_size = loader.batch_size

        if iteration * batch_size % log_interval < batch_size:
            epoch = engine.state.epoch
            max_iters = len(loader)
            progress = 100 * iteration / (max_iters * epochs)
            loss = engine.state.output
            logging.info("Training Progress {:.2f}% || Epoch: {}/{}, Iteration: {}/{}, Loss: {:.4f}"
                         .format(progress, epoch, epochs, iteration % max_iters, max_iters, loss))

    @trainer.on(Events.EPOCH_COMPLETED)
    def _validate(engine):
        eval_planner(extractor, content_planner)

    @trainer.on(Events.COMPLETED)
    def _test(engine):
        eval_planner(extractor, content_planner, test=True)

    trainer.run(loader, epochs)
    logging.info("Finished training process!")

    if not path.exists("models"):
        makedirs("models")
    torch.save(content_planner.state_dict(), "models/content_planner.pt")

    return content_planner.cpu()


def eval_planner(extractor, content_planner, test=False):
    content_planner = content_planner.to(device)
    bleu_metric = BleuScore()
    if test:
        used_set = "Test"
        data = load_planner_data("test", extractor)
        loader = DataLoader(data, batch_size=1)
    else:
        used_set = "Validation"
        data = load_planner_data("valid", extractor)
        loader = DataLoader(data, batch_size=1)

    def _evaluate():
        """
        Logs BLEU score between generated content plans and gold content plans.
        Logs average sizes of content plans.
        """
        content_planner.eval()
        gen_len = 0
        gold_len = 0
        size = 0

        for batch in loader:
            with torch.no_grad():
                records, content_plan = to_device(batch)
                hidden, cell = content_planner.init_hidden(records)
                input_index = torch.tensor([data.vocab[BOS_WORD]], device=device)

                generated_plan = list()
                gold_plan = content_plan[content_plan > data.vocab[PAD_WORD]][1:-1].tolist()

                while len(generated_plan) < content_plan.size(1):
                    output, hidden, cell = content_planner(input_index, hidden, cell)
                    input_index = output.argmax(dim=1)
                    if input_index.cpu() == data.vocab[EOS_WORD]:
                        break
                    generated_plan.append(input_index.item())

                bleu_metric(gold_plan, generated_plan)
                gen_len += len(generated_plan)
                gold_len += len(gold_plan)
                size += 1
        logging.info("{} Results - BLEU Score: {:.4f}".format(used_set, bleu_metric.calculate()))
        logging.info("{} Results - avg gold content plan length: {}".format(used_set, gold_len / size))
        logging.info("{} Results - avg generated content plan length: {}".format(used_set, gen_len / size))

    _evaluate()


def planner_is_available():
    if path.exists("models/content_planner.pt"):
        logging.info("Found saved planner!")
        return True
    else:
        logging.warning("Failed to locate saved planner!")
        return False


def load_planner(extractor):
    if path.exists("models/content_planner.pt"):
        data = load_planner_data("train", extractor)
        content_planner = ContentPlanner(len(data.idx2word))
        content_planner.load_state_dict(torch.load("models/content_planner.pt", map_location="cpu"))
        return content_planner
