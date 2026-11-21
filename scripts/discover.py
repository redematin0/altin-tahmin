#!/usr/bin/env python3
"""Fiyat kaynaklarını keşfeder: hangi adres çalışıyor, ne döndürüyor. Çıktı: data/discover.txt"""
import os, re, sys, json, urllib.request, urllib.parse

DATA = os.environ.get('DATA_DIR', 'data')
UA = {'User-Agent': 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Mobile Safari/537.36',
      'Accept': 'text/html,application/json,*/*', 'Accept-Language': 'tr-TR,tr;q=0.9'}
out = []


def get(url, data=None, headers=None, n=1500):
    try:
        req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
        with urllib.request.urlopen(req, timeout=25) as r:
            b = r.read()
            txt = b.decode('utf-8', 'replace')
            out.append(f'\n===== {url} -> {r.status} {len(b)} bayt {r.headers.get("content-type")}\n{txt[:n]}')
            return txt
    except Exception as e:
        out.append(f'\n===== {url} -> HATA {e}')
        return ''


# AKOD: ana sayfa + script dosyaları + içlerindeki olası veri adresleri
home = get('https://akod.org.tr/', n=4000)
base = 'https://akod.org.tr/'
cands = set()
for src in re.findall(r'<script[^>]+src=["\']([^"\']+)', home):
    u = urllib.parse.urljoin(base, src)
    if 'akod' in u or src.startswith('/') or src.startswith('assets') or src.startswith('js'):
        js = get(u, n=300)
        for m in re.findall(r'["\'`]((?:https?://|/)[^"\'`\s]{3,200})["\'`]', js):
            if re.search(r'api|fiyat|price|altin|gold|json|ajax|\.php|kur', m, re.I):
                cands.add(urllib.parse.urljoin(base, m))
for m in re.findall(r'["\']((?:https?://|/)[^"\'\s]{3,200})["\']', home):
    if re.search(r'api|fiyat|price|altin|gold|json|ajax|\.php', m, re.I):
        cands.add(urllib.parse.urljoin(base, m))
out.append('\n##### AKOD aday adresler:\n' + '\n'.join(sorted(cands)))
for u in sorted(cands)[:25]:
    if not re.search(r'\.(css|png|jpg|svg|woff2?|ico)(\?|$)', u):
        get(u, n=1200)
for p in ['api/prices', 'api/fiyatlar', 'api/altin', 'fiyatlar', 'altin-fiyatlari', 'api/gold-prices']:
    get(base + p, n=800)

# Diğer kaynaklar
get('https://canlipiyasalar.haremaltin.com/tmp/altin.json?dil_kodu=tr', data=b'dil_kodu=tr', headers={'Content-Type': 'application/x-www-form-urlencoded', 'Referer': 'https://canlipiyasalar.haremaltin.com/', 'X-Requested-With': 'XMLHttpRequest'})
get('https://static.altinkaynak.com/public/Gold')
get('https://api.genelpara.com/embed/altin.json')
get('https://anlikaltinfiyatlari.com/altin/antalya', n=600)
get('https://www.anlikfiyat.com.tr/altin/antalya', n=600)
get('https://finans.truncgil.com/today.json', n=600)

os.makedirs(DATA, exist_ok=True)
open(os.path.join(DATA, 'discover.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(x[:300] for x in out))
