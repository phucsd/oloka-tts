import json

nb_path = 'kaggle_worker_remote/vieneu-tts-dual-t4-worker.ipynb'
with open(nb_path, 'r', encoding='utf-8') as f:
    nb = json.load(f)

for cell in nb['cells']:
    if cell['cell_type'] == 'code' and 'bootstrap.py' in cell['source']:
        print('Original cell 3:', repr(cell['source']))
        cell['source'] = '!python bootstrap.py --gateway-url "https://phucsd-vieneu-gateway.hf.space" --auth-token "vieneu_secure_worker_token_2026"\n'
        print('Updated cell 3:', repr(cell['source']))

with open(nb_path, 'w', encoding='utf-8') as f:
    json.dump(nb, f, indent=2)

print('Updated notebook successfully!')
