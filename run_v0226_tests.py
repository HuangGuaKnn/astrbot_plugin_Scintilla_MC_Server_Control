import subprocess,pathlib,json,os
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "tests"))
from _no_window import no_window_kw  # noqa: E402
py=str(pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home()/"AppData"/"Local"))/"AstrBot"/"backend"/"python"/"python.exe"); out=[]
for f in sorted(pathlib.Path('tests').glob('test_*.py')):
 try:
  p=subprocess.run([py,str(f)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120, **no_window_kw())
  out.append({'test':f.name,'exit':p.returncode,'tail':(p.stdout+p.stderr)[-1200:]}); print(f.name,p.returncode)
 except subprocess.TimeoutExpired: out.append({'test':f.name,'exit':'timeout'}); print(f.name,'timeout')
pathlib.Path('../v0226_test_results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
