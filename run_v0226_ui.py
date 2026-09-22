import subprocess,pathlib,json
py=r'C:\Users\10316\AppData\Local\AstrBot\backend\python\python.exe'; out=[]
for f in sorted(pathlib.Path('tests').glob('ui_*.py')):
 try:
  p=subprocess.run([py,str(f)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180)
  out.append({'test':f.name,'exit':p.returncode,'tail':(p.stdout+p.stderr)[-500:]}); print(f.name,p.returncode)
 except subprocess.TimeoutExpired: out.append({'test':f.name,'exit':'timeout'}); print(f.name,'timeout')
pathlib.Path('../v0226_ui_results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
