import subprocess, pathlib, json
py = r'C:\Users\10316\AppData\Local\AstrBot\backend\python\python.exe'


def run(pat, out_name):
    out = []
    for f in sorted(pathlib.Path('tests').glob(pat)):
        try:
            p = subprocess.run([py, str(f)], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=180)
            exit_code = p.returncode
            tail = (p.stdout + p.stderr)[-1500:]
        except subprocess.TimeoutExpired:
            exit_code, tail = 'timeout', ''
        out.append({'test': f.name, 'exit': exit_code, 'tail': tail})
        print(f.name, exit_code, flush=True)
    pathlib.Path('../' + out_name).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')


run('test_*.py', 'v0228_test_results.json')
run('ui_*.py', 'v0228_ui_results.json')
print('DONE')
