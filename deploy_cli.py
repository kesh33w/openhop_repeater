from pathlib import Path
import hashlib, shutil, subprocess, time, os
root=Path('/opt/openhop-production/releases/08d976a.evidence20261110/venv/lib/python3.13/site-packages')
stage=Path('/home/dan/cad-cli-stage')
files={'repeater/handler_helpers/mesh_cli.py':'42c6437538cec55ae777dc706c6363e3e099ce699e1086efec606ebe0fc0b304','repeater/web/api_endpoints.py':'2e39739267685f186e31c6d45c21031b4ce8362a064e9790f6d54a099868d430'}
for f,h in files.items():
 assert hashlib.sha256((root/f).read_bytes()).hexdigest()==h, 'Live baseline changed: '+f
backup=Path('/opt/openhop-production/backups/cad-cli-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()))
for f in files:
 p=root/f; target=backup/f; target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,target)
for f in files:
 p=root/f; temp=p.with_suffix('.py.new'); shutil.copy2(stage/f,temp); os.replace(temp,p)
subprocess.run([str(root.parents[2]/'bin/python'),'-m','compileall','-q',*[str(root/f) for f in files]],check=True)
subprocess.run(['systemctl','restart','openhop-repeater'],check=True)
print('deployed CLI, configuration untouched')
