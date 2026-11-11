import argparse
from pathlib import Path
from .domain import ContentStore,manifest
from .storage import save_json
def initialize(directory):
    directory=Path(directory)
    if (directory/'seed.json').exists():raise ValueError('目录已经初始化')
    store=ContentStore(directory/'objects')
    shared=store.put({'kind':'data','values':[1,2,3]})
    only_a=store.put({'kind':'data','values':[4,5]})
    only_b=store.put({'kind':'data','values':[6,7]})
    orphan=store.put({'kind':'data','values':[8,9]})
    orphan_b=store.put({'kind':'data','values':[10,11]})
    a=store.put(manifest([shared,only_a]));b=store.put(manifest([shared,only_b]))
    names={'shared':shared,'only-a':only_a,'only-b':only_b,'orphan':orphan,'orphan-b':orphan_b,'manifest-a':a,'manifest-b':b}
    save_json(directory/'seed.json',names);return names
if __name__=='__main__':
    parser=argparse.ArgumentParser(description='建立合成内容库');parser.add_argument('directory');args=parser.parse_args();print(initialize(args.directory))
