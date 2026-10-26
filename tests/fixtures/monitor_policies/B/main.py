from .helper import VALUE

def initialize(config): return {'calls':0, 'config':config}
def policy(context,state,action_chunk,images,config):
    assert all(not image.flags.writeable for image in images.values())
    context['calls']+=1
    sums=[int(images[name].sum()) for name in ('head','left_wrist','right_wrist')]
    return [[VALUE,context['calls'],*sums]+[0]*9]
def reset_context(context): context['calls']=0
def release(context): pass

def monitor(context):
    import os, sys
    return {"calls": int(context["calls"]), "python": sys.version.split()[0],
            "pid": os.getpid(), "policy": context["config"]["id"]}
