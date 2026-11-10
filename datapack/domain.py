import hashlib
import re
from pathlib import Path
from .storage import canonical,atomic_write,read_json
class ContentStore:
    def __init__(self,root):self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
    def path(self,sha):
        if not re.fullmatch('[0-9a-f]{64}',sha):raise ValueError('对象地址无效')
        return self.root/sha[:2]/sha[2:]
    def put(self,value):
        data=canonical(value);sha=hashlib.sha256(data).hexdigest();path=self.path(sha)
        if path.exists():
            if path.read_bytes()!=data:raise ValueError('对象地址内容冲突')
        else:atomic_write(path,data)
        return sha
    def read(self,sha):
        path=self.path(sha);data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=sha:raise ValueError('对象摘要损坏')
        return read_json(path)
    def addresses(self):
        return sorted(p.parent.name+p.name for p in self.root.glob('*/*') if p.is_file() and re.fullmatch('[0-9a-f]{64}',p.parent.name+p.name))
def manifest(refs):
    if len(refs)!=len(set(refs)):raise ValueError('清单引用重复')
    return {'kind':'manifest','refs':list(refs)}
