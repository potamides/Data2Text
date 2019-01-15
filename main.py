###############################################################################
# Control room to acces Information Extraction, Content Selection & Planning  #
# and Text Generation Module
###############################################################################

from extractor import get_extractor, eval_extractor
from planner import train_planner

extractor = get_extractor()
eval_extractor(extractor, test=True)
train_planner(extractor)
