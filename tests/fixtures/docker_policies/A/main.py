type ImageStats = list[int]
TEMPLATE = t"Python 3.14"
from .helper import VALUE
import time
import numpy as np

def initialize(config): return {'calls':0}
def policy(context,state,action_chunk,images,config):
    assert all(not image.flags.writeable for image in images.values())
    time.sleep(config.get('params', {}).get('delay', 0))
    for name, value in state.get('image_values', {}).items():
        assert np.all(images[name] == value), name
    context['calls']+=1
    sums=[int(images[name].sum()) for name in ('head','left_wrist','right_wrist')]
    return [[VALUE,context['calls'],*sums]+[0]*9]
def reset_context(context): context['calls']=0
def release(context): pass
