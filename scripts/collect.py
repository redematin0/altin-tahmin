#!/usr/bin/env python3
"""Altın Tahmin — sunucu tarafı.

GitHub Actions her çalıştığında:
  1. Fiyat geçmişini çeker (ons, USD/TRY, dolar endeksi, ABD 10 yıllık faiz, gümüş, VIX)
  2. Kapalıçarşı alış/satış fiyatlarını çeker (gram, çeyrek, yarım, tam, Cumhuriyet)
  3. Haberleri toplar, puanlar ve günlük hafızaya ekler
  4. Ekonomik takvimi günceller (Fed, TCMB, ABD enflasyon/istihdam)
  5. Birkaç tahmin yöntemini geçmişte sınar, en iyisini seçer, tahmin üretir
  6. Kendi tahmin günlüğünü tutar ve sapmalarından öğrenir
  7. Telegram botunu çalıştırır (özet, uyarılar, komutlar)

Çıktılar data/ klasörüne JSON olarak yazılır; uygulama (index.html) bunları okur.
Ortam değişkenleri: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (isteğe bağlı),
ANTHROPIC_API_KEY (isteğe bağlı, haber puanlaması için), OFFLINE=1 (test).
"""
import json, math, os, re, sys, time, datetime as dt, email.utils, urllib.request, urllib.parse, xml.etree.ElementTree as ET
import numpy as np
try:
    from statsforecast.models import AutoARIMA, AutoETS
    HAS_SF = True
except Exception:
    HAS_SF = False
try:
    from arch import arch_model
    HAS_ARCH = True
except Exception:
    HAS_ARCH = False

DATA = os.environ.get('DATA_DIR', 'data')
OZ = 31.1035
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36', 'Accept': '*/*'}
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date().isoformat()
TRT = dt.timezone(dt.timedelta(hours=3))
OFFLINE = os.environ.get('OFFLINE') == '1'
LOGS = []


def log(*a):
    msg = ' '.join(str(x) for x in a)
    LOGS.append(msg)
    print(msg, flush=True)


def load(name, default):
    try:
        with open(os.path.join(DATA, name), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save(name, obj):
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, name), 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))


def http(url, data=None, headers=None, timeout=25, tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def get_json(url, **kw):
    return json.loads(http(url, **kw).decode('utf-8'))


# ---------------------------------------------------------------- 1. FİYATLAR
YAHOO = {'ons': 'GC=F', 'kur': 'USDTRY=X', 'dxy': 'DX-Y.NYB', 'tnx': '^TNX', 'silver': 'SI=F', 'vix': '^VIX'}


def yahoo(sym, rng='5y'):
    q = urllib.parse.quote(sym)
    err = None
    for host in ('query1', 'query2'):
        try:
            j = json.loads(http(f'https://{host}.finance.yahoo.com/v8/finance/chart/{q}?range={rng}&interval=1d&includePrePost=false', tries=1).decode())
            r = j['chart']['result'][0]
            ts, cl = r['timestamp'], r['indicators']['quote'][0]['close']
            out = {}
            for t, c in zip(ts, cl):
                if c is not None and c > 0:
                    out[dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat()] = float(c)
            live = r.get('meta', {}).get('regularMarketPrice')
            return out, float(live) if live else None
        except Exception as e:
            err = e
    raise err


BINANCE = ('https://data-api.binance.vision', 'https://api.binance.com')


def binance_daily(sym, pages=3):
    """Binance günlük kapanışları (en fazla ~3000 gün), yalnızca hafta içi günler."""
    out, end = {}, None
    for _ in range(pages):
        j = None
        for host in BINANCE:
            try:
                j = get_json(f'{host}/api/v3/klines?symbol={sym}&interval=1d&limit=1000' + (f'&endTime={end}' if end else ''))
                break
            except Exception as e:
                last = e
        if j is None:
            if out:
                break
            raise last
        if not j:
            break
        for k in j:
            d = dt.datetime.fromtimestamp(k[0] / 1000, dt.timezone.utc).date()
            if d.weekday() < 5:
                out[d.isoformat()] = float(k[4])
        end = j[0][0] - 1
        if len(j) < 1000:
            break
    return out


def binance_price(sym):
    for host in BINANCE:
        try:
            return float(get_json(f'{host}/api/v3/ticker/price?symbol={sym}')['price'])
        except Exception:
            pass
    return None


def fred(series_id):
    start = (NOW - dt.timedelta(days=365 * 9)).date().isoformat()
    txt = http(f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}', timeout=60, tries=1).decode('utf-8')
    out = {}
    for line in txt.strip().splitlines()[1:]:
        d, _, v = line.partition(',')
        try:
            out[d] = float(v)
        except ValueError:
            pass
    return out


FRED = {'dxy': 'DTWEXBGS', 'tnx': 'DGS10', 'vix': 'VIXCLS'}


def fetch_history():
    series, live, errs = {}, {}, []
    # 1) Ana fiyatlar: Binance (PAXG = 1 ons altına bağlı, USDT/TRY ≈ dolar kuru); olmazsa Yahoo
    for k, sym, ysym in (('ons', 'PAXGUSDT', 'GC=F'), ('kur', 'USDTTRY', 'USDTRY=X')):
        try:
            series[k] = binance_daily(sym); live[k] = binance_price(sym)
            log(f'binance {sym}: {len(series[k])} gün')
        except Exception as e:
            errs.append(f'{sym}: {e}')
            try:
                series[k], live[k] = yahoo(ysym)
                log(f'yahoo {ysym}: {len(series[k])} gün')
            except Exception as e2:
                errs.append(f'{ysym}: {e2}')
    if 'ons' not in series or 'kur' not in series:
        raise RuntimeError('Temel fiyat verisi alınamadı: ' + '; '.join(errs))
    # 2) Yardımcı göstergeler: FRED (dolar endeksi, ABD 10 yıllık faiz, VIX); gümüş Yahoo'dan denenir
    cache = load('factors.json', {})
    for k, sid in list(FRED.items()) + [('silver', 'SI=F')]:
        c = cache.get(k)
        if c and c.get('day') == TODAY:
            series[k] = c['data']; continue
        try:
            series[k] = fred(sid) if k != 'silver' else yahoo(sid)[0]
            cache[k] = {'day': TODAY, 'data': series[k]}
            log(f'{sid}: {len(series[k])} gün')
        except Exception as e:
            log(sid, 'alınamadı:', e)
            if c:
                series[k] = c['data']  # dünkü veriyle devam
    save('factors.json', cache)
    days = sorted(set(series['ons']) & set(series['kur']))
    hist = {'dates': days}
    for k in YAHOO:
        s, arr, last = series.get(k, {}), [], None
        for d in days:
            last = s.get(d, last)
            arr.append(last)
        hist[k] = arr
    return hist, live


# ---------------------------------------------------------------- 2. KAPALIÇARŞI
def tr_num(v):
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace('%', '').replace('₺', '').replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'\d{1,3}(\.\d{3})+', s):  # 6.300 → 6300 (Türkçe binlik ayırıcı)
        s = s.replace('.', '')
    elif ',' in s:
        s = s.replace(',', '.')
    return float(s)


def norm(s):
    s = s.lower()
    for a, b in zip('çğıöşüâ', 'cgiosua'):
        s = s.replace(a, b)
    return re.sub(r'[^a-z0-9]', '', s)


KAPALI = {'gram': ['gra', 'gramaltin', 'gramaltn'], 'ceyrek': ['ceyrekaltin', 'ceyrek'], 'yarim': ['yarimaltin', 'yarim'],
          'tam': ['tamaltin', 'tam'], 'cumhuriyet': ['cumhuriyetaltini', 'cumhuriyet'], 'ata': ['ataaltin', 'ata'],
          'b22': ['22ayarbilezik', 'yia', '22ayar', 'bilezik']}


def fetch_kapali():
    for url in ('https://finans.truncgil.com/v4/today.json', 'https://finans.truncgil.com/today.json', 'https://finans.truncgil.com/v3/today.json'):
        try:
            j = get_json(url)
        except Exception as e:
            log('kapalıçarşı', url, e)
            continue
        idx = {norm(k): v for k, v in j.items() if isinstance(v, dict)}
        out = {}
        for name, keys in KAPALI.items():
            for k in keys:
                v = idx.get(k)
                if not v:
                    continue
                vv = {norm(a): b for a, b in v.items()}
                try:
                    al = tr_num(vv.get('buying') or vv.get('alis'))
                    sat = tr_num(vv.get('selling') or vv.get('satis'))
                    if al > 0 and sat > 0:
                        out[name] = {'al': al, 'sat': sat}
                        break
                except Exception:
                    pass
        if 'gram' in out:
            meta = j.get('Meta_Data') if isinstance(j.get('Meta_Data'), dict) else {}
            upd = j.get('Update_Date') or meta.get('Update_Date')
            log('kapalıçarşı:', ', '.join(out))
            return {'kaynak': url.split('/')[2], 'guncelleme': upd, 'fiyat': out}
    return None


SRC_AD = {'akod': 'AKOD (Antalya Kuyumcular Odası)', 'kapali': 'Kapalıçarşı', 'altinkaynak': 'Altınkaynak'}
PROD_AD = {'gram': 'Gram altın (24 ayar)', 'b22': '22 ayar (gram)', 'ceyrek': 'Çeyrek', 'yarim': 'Yarım', 'tam': 'Tam', 'cumhuriyet': 'Cumhuriyet'}


def fetch_akod():
    """Antalya Kuyumcular Odası tavsiye satış fiyatları (akod.org.tr/api/prices)."""
    j = get_json('https://akod.org.tr/api/prices')
    cmap = {'B_T': 'b22', 'C': 'ceyrek', 'Y': 'yarim', 'T': 'tam', 'A': 'cumhuriyet'}
    out = {}
    for p in j.get('prices', []):
        k = cmap.get(p.get('code'))
        if k and p.get('price') and p.get('currency') == '₺':
            out[k] = {'al': None, 'sat': float(p['price'])}
    log('AKOD:', ', '.join(f'{k} {v["sat"]:.0f}' for k, v in out.items()))
    return {'fiyat': out, 'guncelleme': j.get('updatedAt')}


def fetch_altinkaynak():
    j = get_json('https://static.altinkaynak.com/public/Gold')
    log('Altınkaynak ürünleri:', '; '.join(f"{x.get('Kod')}={x.get('Aciklama')}" for x in j)[:600])
    out = {}
    for x in j:
        a = norm(x.get('Aciklama', '')); k = None
        if x.get('Kod') == 'HH_T' or a == 'has':
            k = 'gram'
        elif 'ceyrek' in a and 'eski' not in a:
            k = 'ceyrek'
        elif 'yarim' in a and 'eski' not in a:
            k = 'yarim'
        elif a.startswith('tam') and 'eski' not in a:
            k = 'tam'
        elif 'cumhuriyet' in a or a in ('ata', 'ataaltin', 'ataliras'):
            k = 'cumhuriyet'
        elif '22' in a and ('ayar' in a or 'bilezik' in a):
            k = 'b22'
        if k and k not in out:
            try:
                out[k] = {'al': tr_num(x['Alis']), 'sat': tr_num(x['Satis'])}
            except Exception:
                pass
    return {'fiyat': out, 'guncelleme': j[0].get('GuncellenmeZamani') if j else None}


def collect_sources(kap):
    src = {}
    if kap and kap.get('fiyat'):
        src['kapali'] = {'fiyat': kap['fiyat'], 'guncelleme': kap.get('guncelleme')}
    for k, fn in (('akod', fetch_akod), ('altinkaynak', fetch_altinkaynak)):
        try:
            r = fn()
            if r['fiyat']:
                src[k] = r
        except Exception as e:
            log(k, 'alınamadı:', e)
    return src


def source_stats(src, store):
    """Kaynaklar arası fiyat farkları: anlık en ucuz, Kapalıçarşı'ya göre ortalama fark, alış-satış makası, kaç kez en ucuz."""
    hist = store.setdefault('hist', [])
    if src:
        hist.append({'t': NOW.isoformat(timespec='minutes'), 'p': {s: {k: [v.get('al'), v['sat']] for k, v in d['fiyat'].items()} for s, d in src.items()}})
    cut = (NOW - dt.timedelta(days=120)).isoformat()
    store['hist'] = hist = [h for h in hist if h['t'] >= cut]
    store['latest'] = {s: {**d, 'zaman': NOW.isoformat(timespec='minutes')} for s, d in src.items()}
    stats = {}
    for prod in PROD_AD:
        now = {s: d['fiyat'][prod] for s, d in src.items() if prod in d['fiyat']}
        st = {'now': {s: v for s, v in now.items()}, 'src': {}}
        if now:
            st['cheapest'] = min(now, key=lambda s: now[s]['sat'])
        recent = [h for h in hist if h['t'] >= (NOW - dt.timedelta(days=30)).isoformat()]
        for s in SRC_AD:
            diffs, spreads, wins, n = [], [], 0, 0
            for h in recent:
                p = h['p']
                if prod not in p.get(s, {}):
                    continue
                sat = p[s][prod][1]; al = p[s][prod][0]
                kc = p.get('kapali', {}).get(prod)
                if kc and s != 'kapali':
                    diffs.append(sat / kc[1] - 1)
                if al:
                    spreads.append((sat - al) / sat)
                cands = {x: p[x][prod][1] for x in p if prod in p[x]}
                if len(cands) >= 2:
                    n += 1; wins += min(cands, key=cands.get) == s
            if diffs or spreads or n:
                st['src'][s] = {'farkKC': float(np.mean(diffs)) if diffs else None, 'makas': float(np.mean(spreads)) if spreads else None,
                                'enUcuzOran': wins / n if n else None, 'n': len(diffs) or n}
        stats[prod] = st
    store['stats'] = stats
    store['adlar'] = {'kaynak': SRC_AD, 'urun': PROD_AD}
    return store


def antalya_ratio(src):
    """AKOD ziynet fiyatlarının Kapalıçarşı satışına oranı: Antalya'nın İstanbul'a göre ne kadar farklı olduğu."""
    a, k = src.get('akod', {}).get('fiyat', {}), src.get('kapali', {}).get('fiyat', {})
    r = [a[p]['sat'] / k[p]['sat'] for p in ('ceyrek', 'yarim', 'tam') if p in a and p in k and k[p]['sat']]
    if not r:
        return None
    v = float(np.median(r))
    return v if 0.93 < v < 1.08 else None


def build_report(L, P):
    """Tahmin–gerçekleşen karşılaştırma raporu."""
    rep = {}
    for h, name in HZ:
        done = sorted([x for x in L if x['h'] == h and x.get('act') is not None], key=lambda x: x['d'])
        if not done:
            rep[str(h)] = {'n': 0, 'name': name}; continue
        err = [x['act'] - x['med'] for x in done]
        last = done[-30:]; e30 = [x['act'] - x['med'] for x in last]
        rep[str(h)] = {'name': name, 'n': len(done), 'mae': float(np.mean(np.abs(e30))), 'bias': float(np.mean(e30)),
                       'maeAll': float(np.mean(np.abs(err))), 'yon': float(np.mean([(x['act'] > 0) == (x['med'] > 0) for x in last])),
                       'aralik': float(np.mean([x['lo'] <= x['act'] <= x['hi'] for x in last])),
                       'trend': (float(np.mean(np.abs(e30[-5:]))) if len(e30) >= 10 else None, float(np.mean(np.abs(e30[:5]))) if len(e30) >= 10 else None),
                       'son': [{k: x.get(k) for k in ('d', 'dAct', 'med', 'lo', 'hi', 'act', 'p')} for x in done[-15:]]}
    # okunabilir özet
    lines = []
    r1 = rep.get('1', {})
    if r1.get('n'):
        x = r1['son'][-1]
        lines.append(f"Son 24 saatlik tahmin ({x['d']}): {fmt_pct(x['med'], 2)} demiştik, gerçekleşen {fmt_pct(x['act'], 2)}; fark {fmt_pct(x['act'] - x['med'], 2)}.")
    for h, name in HZ:
        r = rep[str(h)]
        if r.get('n', 0) >= 3:
            yon = 'olduğundan düşük' if r['bias'] > 0.0005 else 'olduğundan yüksek' if r['bias'] < -0.0005 else 'dengeli'
            lines.append(f"{name}: son {min(30, r['n'])} tahminde ortalama sapma %{abs(r['mae']) * 100:.2f}".replace('.', ',') + f"; model fiyatı {yon} tahmin ediyor"
                         f"{'' if yon == 'dengeli' else ' ve bunu otomatik düzeltiyor'}. Yön isabeti %{r['yon'] * 100:.0f}, aralık isabeti %{r['aralik'] * 100:.0f}.")
            t = r['trend']
            if t[0] is not None:
                lines.append(f"{name}: sapma ilk tahminlerde %{t[1] * 100:.2f} iken son tahminlerde %{t[0] * 100:.2f}".replace('.', ',') + f" — {'iyileşiyor' if t[0] < t[1] else 'iyileşmiyor'}.")
        elif r.get('n', 0):
            lines.append(f"{name}: {r['n']} sonuç var; güvenilir istatistik için en az 3 gerekiyor.")
    if not lines:
        lines.append('Henüz süresi dolmuş tahmin yok. İlk 24 saatlik sonuç yarın gelecek; model sapmasını ölçüp kendini bundan sonra düzeltecek.')
    rep['ozet'] = lines
    return rep


# ---------------------------------------------------------------- 3. HABERLER
FEEDS = [
    'https://news.google.com/rss/search?q=alt%C4%B1n+fiyatlar%C4%B1&hl=tr&gl=TR&ceid=TR:tr',
    'https://news.google.com/rss/search?q=ons+alt%C4%B1n+Fed&hl=tr&gl=TR&ceid=TR:tr',
    'https://news.google.com/rss/search?q=dolar+TL+kur+TCMB&hl=tr&gl=TR&ceid=TR:tr',
    'https://news.google.com/rss/search?q=gold+price&hl=en-US&gl=US&ceid=US:en',
    'https://news.google.com/rss/search?q=Fed+rate+dollar+gold&hl=en-US&gl=US&ceid=US:en',
]
POS = ['rekor', 'yükseldi', 'yükseliş', 'yükseliyor', 'yükselerek', 'arttı', 'tırman', 'zirve', 'faiz indirim', 'indirime', 'güvenli liman', 'jeopolitik', 'gerilim', 'savaş', 'saldırı', 'belirsizlik', 'dolar zayıf', 'zayıf dolar', 'merkez bankaları alım', 'talep art', 'rate cut', 'cuts', 'dovish', 'surge', 'soar', 'rally', 'record', 'jump', 'climb', 'safe haven', 'tension', 'war', 'attack', 'uncertainty', 'weak dollar', 'central bank buying', 'gains', 'rises', 'higher']
NEG = ['düştü', 'düşüş', 'düşüyor', 'düşerek', 'geriledi', 'geriliyor', 'geri çekil', 'değer kaybetti', 'sert kayıp', 'faiz artır', 'şahin', 'dolar güçlen', 'güçlü dolar', 'kâr satış', 'kar satış', 'ateşkes', 'barış', 'sakinleş', 'rate hike', 'hawkish', 'slump', 'fall', 'falls', 'drop', 'slide', 'tumble', 'strong dollar', 'profit-taking', 'profit taking', 'ceasefire', 'peace', 'lower', 'loses', 'retreat']


def tr_lower(s):
    return s.replace('I', 'ı').replace('İ', 'i').lower()


def score_kw(t):
    s = ' ' + re.sub(r'[^\wçğıöşü\- ]', ' ', tr_lower(t)) + ' '
    hit = lambda w: (' ' + w) in s if len(w) > 5 or ' ' in w else re.search(r'\b' + re.escape(w) + r'\b', s) is not None
    if '?' in t:  # soru başlıkları ("düşecek mi?") yön belirtmez
        return 0.0
    p = sum(hit(w) for w in POS)
    n = sum(hit(w) for w in NEG)
    return (p - n) / (p + n) if p + n else 0.0


def score_ai(titles):
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key or not titles:
        return None
    prompt = ('Aşağıdaki haber başlıklarının her birini, gram altının TL fiyatı için önümüzdeki günlerdeki etkisine göre -1 (çok olumsuz) '
              'ile +1 (çok olumlu) arasında puanla. Ons altını, doları ve TL kurunu etkileyen gelişmeleri dikkate al. '
              'Yalnızca sayılardan oluşan bir JSON dizisi döndür.\n' + '\n'.join(f'{i + 1}. {t}' for i, t in enumerate(titles)))
    try:
        j = get_json('https://api.anthropic.com/v1/messages', data=json.dumps({'model': 'claude-haiku-5-5', 'max_tokens': 400, 'messages': [{'role': 'user', 'content': prompt}]}).encode(),
                     headers={'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'}, timeout=60)
        txt = ''.join(c.get('text', '') for c in j['content'])
        arr = json.loads(txt[txt.index('['):txt.rindex(']') + 1])
        return [max(-1.0, min(1.0, float(x))) for x in arr]
    except Exception as e:
        log('AI puanlama hatası', e)
        return None


def fetch_news(store):
    items, seen = [], set()
    for f in FEEDS:
        try:
            root = ET.fromstring(http(f))
        except Exception as e:
            log('haber', f[:60], e)
            continue
        for it in root.iter('item'):
            raw = (it.findtext('title') or '').strip()
            src = it.findtext('source') or ''
            title = re.sub(r'\s+-\s+[^-]+$', '', raw).strip() if src or ' - ' in raw else raw
            if not src:
                m = re.search(r'-\s+([^-]+)$', raw)
                src = m.group(1).strip() if m else ''
            key = title.lower()[:60]
            if not title or key in seen:
                continue
            seen.add(key)
            try:
                t = email.utils.parsedate_to_datetime(it.findtext('pubDate')).astimezone(dt.timezone.utc)
            except Exception:
                t = NOW
            if (NOW - t).days > 10:
                continue
            items.append({'t': title, 'src': src.strip(), 'link': it.findtext('link') or '', 'time': t.isoformat(timespec='minutes'), 's': round(score_kw(title), 2)})
    items.sort(key=lambda x: x['time'], reverse=True)
    # AI puanı (varsa) — önbellekli
    cache = store.setdefault('ai', {})
    todo = [x['t'] for x in items if x['t'] not in cache][:40]
    sc = score_ai(todo)
    if sc:
        for t, s in zip(todo, sc):
            cache[t] = round(s, 2)
    for x in items:
        if x['t'] in cache:
            x['s'] = cache[x['t']]; x['ai'] = True
    if len(cache) > 1500:
        for k in list(cache)[:len(cache) - 1500]:
            del cache[k]
    # günlük hafızaya ekle (eski kayıtlar güncel puanlayıcıyla yeniden puanlanır)
    days = store.setdefault('days', {})
    for h in days.values():
        for y in h['items']:
            y['s'] = cache.get(y['t'], round(score_kw(y['t']), 2))
    for x in items:
        d = x['time'][:10]
        h = days.setdefault(d, {'items': []})
        if not any(y['t'] == x['t'] for y in h['items']):
            h['items'].append({k: x[k] for k in ('t', 's', 'src', 'link')})
        h['items'].sort(key=lambda y: abs(y['s']), reverse=True)
        h['items'] = h['items'][:10]
        h['score'] = round(sum(y['s'] for y in h['items']) / len(h['items']), 3)
    for d in sorted(days)[:-800]:
        del days[d]
    num = den = 0.0
    for x in items:
        age = (NOW - dt.datetime.fromisoformat(x['time'])).total_seconds() / 86400
        w = 0.5 ** (max(0, age) / 2)
        num += w * x['s']; den += w
    store['latest'] = items[:20]
    store['score'] = round(max(-1, min(1, num / den * 1.5)), 3) if den else 0
    store['updated'] = NOW.isoformat(timespec='minutes')
    log(f'haber: {len(items)} başlık, skor {store["score"]}, AI {bool(sc)}')
    return store


# ---------------------------------------------------------------- 4. TAKVİM
FOMC_PAST = ['2022-03-16', '2022-05-04', '2022-06-15', '2022-07-27', '2022-09-21', '2022-11-02', '2022-12-14',
             '2023-02-01', '2023-03-22', '2023-05-03', '2023-06-14', '2023-07-26', '2023-09-20', '2023-11-01', '2023-12-13',
             '2024-01-31', '2024-03-20', '2024-05-01', '2024-06-12', '2024-07-31', '2024-09-18', '2024-11-07', '2024-12-18',
             '2025-01-29', '2025-03-19', '2025-05-07', '2025-06-18', '2025-07-30', '2025-09-17', '2025-10-29', '2025-12-10',
             '2026-01-28', '2026-03-18', '2026-04-29', '2026-06-17', '2026-07-29', '2026-09-16', '2026-10-28']
# Kaynak: federalreserve.gov (12 Kasım 2026 revize takvim) ve tcmb.gov.tr PPK takvimi
FIXED = [('2026-12-09', 'Fed faiz kararı', 'fed'),
         ('2027-01-27', 'Fed faiz kararı', 'fed'), ('2027-03-17', 'Fed faiz kararı', 'fed'), ('2027-04-28', 'Fed faiz kararı', 'fed'),
         ('2027-06-09', 'Fed faiz kararı', 'fed'), ('2027-07-28', 'Fed faiz kararı', 'fed'), ('2027-09-15', 'Fed faiz kararı', 'fed'),
         ('2027-11-17', 'Fed faiz kararı', 'fed'),
         ('2026-12-10', 'TCMB faiz kararı', 'tcmb'), ('2027-01-21', 'TCMB faiz kararı', 'tcmb'), ('2027-03-18', 'TCMB faiz kararı', 'tcmb'),
         ('2027-04-22', 'TCMB faiz kararı', 'tcmb'), ('2027-06-10', 'TCMB faiz kararı', 'tcmb')]
FF_NAMES = {'CPI': 'ABD enflasyonu (TÜFE)', 'Non-Farm': 'ABD tarım dışı istihdam', 'Core PCE': 'ABD PCE enflasyonu', 'FOMC': 'Fed', 'Federal Funds Rate': 'Fed faiz kararı',
            'GDP': 'ABD büyüme', 'Powell': 'Fed başkanı konuşması', 'Warsh': 'Fed başkanı konuşması', 'Retail Sales': 'ABD perakende satışlar', 'PPI': 'ABD ÜFE'}


def fetch_calendar(cal):
    ev = {(e['date'], e['title']): e for e in cal.get('events', []) if e['date'] >= (NOW - dt.timedelta(days=60)).date().isoformat()}
    for d, t, k in FIXED:
        ev[(d, t)] = {'date': d, 'title': t, 'kind': k}
    try:
        for e in get_json('https://nfs.faireconomy.media/ff_calendar_thisweek.json'):
            if e.get('impact') != 'High' or e.get('country') not in ('USD', 'TRY'):
                continue
            title = e.get('title', '')
            name = next((v for k, v in FF_NAMES.items() if k.lower() in title.lower()), None)
            if not name and e.get('country') == 'TRY':
                name = 'Türkiye: ' + title
            if not name:
                continue
            when = dt.datetime.fromisoformat(e['date']).astimezone(TRT)
            d = when.date().isoformat()
            if any(x['date'] == d and x['kind'] in ('fed', 'tcmb') and 'faiz' in x['title'] and 'faiz' in name for x in ev.values()):
                continue
            ev[(d, name)] = {'date': d, 'title': name, 'kind': 'veri', 'saat': when.strftime('%H:%M')}
        log('takvim: ForexFactory haftalık takvimi eklendi')
    except Exception as e:
        log('takvim', e)
    cal['events'] = sorted(ev.values(), key=lambda e: (e['date'], e['title']))
    return cal


# ---------------------------------------------------------------- 5. MODEL
PHI = 0.97  # oynaklığın uzun dönem ortalamasına dönüş hızı (yarı ömür ≈ 23 işlem günü)
HZ = [(1, '24 saat'), (5, '1 hafta'), (21, '1 ay')]


def prep(H):
    ons, kur = np.array(H['ons'], float), np.array(H['kur'], float)
    ro, rk = np.diff(np.log(ons)), np.diff(np.log(kur))
    L = 0.94
    vo, vk = np.empty(len(ro) + 1), np.empty(len(rk) + 1)
    a, b = np.mean(ro[:30] ** 2), np.mean(rk[:30] ** 2)
    for i in range(len(ro)):
        vo[i], vk[i] = a, b
        a = L * a + (1 - L) * ro[i] ** 2
        b = L * b + (1 - L) * rk[i] ** 2
    vo[-1], vk[-1] = a, b

    def arr(k):
        x = np.array([np.nan if v is None else v for v in H.get(k, [None] * len(ons))], float)
        # eksikleri ileri/geri doldur
        if np.all(np.isnan(x)):
            return None
        idx = np.where(~np.isnan(x), np.arange(len(x)), 0)
        np.maximum.accumulate(idx, out=idx)
        x = x[idx]
        first = np.nanargmax(~np.isnan(x)) if np.isnan(x[0]) else 0
        x[:first] = x[first]
        return x
    return {'ro': ro, 'rk': rk, 'vo': vo, 'vk': vk, 'ons': ons, 'kur': kur,
            'dxy': arr('dxy'), 'tnx': arr('tnx'), 'silver': arr('silver'), 'vix': arr('vix'), 'dates': H['dates']}


def m(a, s, e):
    s = max(0, s)
    return float(np.mean(a[s:e])) if e > s else 0.0


def feats(P, t):
    """t gününün kapanışında bilinen özellikler (ons getirisini açıklamak için)."""
    o = np.log(P['ons'])
    f = [o[t] - o[max(0, t - 5)], o[t] - o[max(0, t - 21)]]
    for k, lag in (('dxy', 5), ('dxy', 21)):
        x = P[k]
        f.append(0.0 if x is None else math.log(x[t] / x[max(0, t - lag)]))
    x = P['tnx']; f.append(0.0 if x is None else (x[t] - x[max(0, t - 21)]) / 10)
    x = P['vix']
    if x is None:
        f.append(0.0)
    else:
        w = x[max(0, t - 252):t + 1]; f.append((x[t] - w.mean()) / (w.std() + 1e-9))
    x = P['silver']
    if x is None:
        f.append(0.0)
    else:
        r = np.log(P['ons'][max(0, t - 252):t + 1] / x[max(0, t - 252):t + 1]); f.append((r[-1] - r.mean()) / (r.std() + 1e-9))
    return f


_FCACHE = {}


def factor_mu(P, e, h):
    """Ridge regresyonu: özelliklerden önümüzdeki h günlük ons getirisini tahmin eder. e = bilinen getiri sayısı."""
    key = (e // 10, h)
    if key not in _FCACHE:
        X, y = [], []
        o = np.log(P['ons'])
        for t in range(max(260, e - 750), e - h + 1):
            X.append(feats(P, t)); y.append(o[t + h] - o[t])
        if len(X) < 120:
            _FCACHE[key] = None
        else:
            X, y = np.array(X), np.array(y)
            mu, sd = X.mean(0), X.std(0) + 1e-9
            Z = (X - mu) / sd
            lam = len(y) * 2.0  # güçlü küçültme: zayıf sinyale aşırı güvenme
            beta = np.linalg.solve(Z.T @ Z + lam * np.eye(Z.shape[1]), Z.T @ (y - y.mean()))
            _FCACHE[key] = (mu, sd, beta)
    c = _FCACHE[key]
    if c is None:
        return 0.0
    mu, sd, beta = c
    z = (np.array(feats(P, e)) - mu) / sd
    return float(z @ beta) / h


_SCACHE, _GCACHE = {}, {}


def stat_mu(P, e, h):
    """statsforecast: ons için AutoARIMA, kur için AutoETS. 20 günde bir yeniden kurulur (önbellek)."""
    key = e // 20
    if key not in _SCACHE:
        lo = np.log(P['ons'][max(0, e - 500):e + 1]); lk = np.log(P['kur'][max(0, e - 500):e + 1])
        try:
            fo = np.asarray(AutoARIMA().forecast(y=lo, h=21)['mean']) - lo[-1]
            fk = np.asarray(AutoETS(model='ZZZ').forecast(y=lk, h=21)['mean']) - lk[-1]
        except Exception as ex:
            log('statsforecast hata', ex); fo = fk = np.zeros(21)
        _SCACHE[key] = (fo, fk)
    fo, fk = _SCACHE[key]
    return float(np.clip(fo[h - 1] / h, -0.003, 0.003)), float(np.clip(fk[h - 1] / h, -0.002, 0.005))


def garch_sigma(P, e, h):
    """arch: Student-t GARCH(1,1) ile ons ve kur için gün gün oynaklık tahmini.
    Parametreler 20 günde bir tahmin edilir, aradaki günlerde sabit parametreyle güncellenir."""
    key = e // 20
    if key not in _GCACHE:
        prm = {}
        for k in ('ro', 'rk'):
            y = P[k][max(0, e - 1000):e] * 100
            try:
                prm[k] = arch_model(y, mean='Zero', vol='GARCH', p=1, q=1, dist='t', rescale=False).fit(disp='off', show_warning=False).params
            except Exception as ex:
                log('arch hata', ex); prm[k] = None
        _GCACHE[key] = prm
    out = []
    for k, v in (('ro', 'vo'), ('rk', 'vk')):
        p = _GCACHE[key][k]
        if p is None:
            out.append(np.full(h, math.sqrt(P[v][e]))); continue
        y = P[k][max(0, e - 1000):e] * 100
        r = arch_model(y, mean='Zero', vol='GARCH', p=1, q=1, dist='t', rescale=False).fix(p)
        var = np.asarray(r.forecast(horizon=h, reindex=False).variance)[-1] / 1e4
        out.append(np.sqrt(var[:h]))
    return out


def kur_mu(P, e):
    return 0.7 * m(P['rk'], e - 21, e) + 0.3 * m(P['rk'], e - 126, e)


METHODS = {
    'denge': ('Dengeli', lambda P, e, h: (0.25 * m(P['ro'], e - 252, e), kur_mu(P, e))),
    'momentum': ('Momentum', lambda P, e, h: (0.5 * m(P['ro'], e - 20, e), kur_mu(P, e))),
    'sabit': ('Sadece kur trendi', lambda P, e, h: (0.0, m(P['rk'], e - 21, e))),
    'uzun': ('Uzun trend', lambda P, e, h: (0.5 * m(P['ro'], e - 252, e), m(P['rk'], e - 252, e))),
    'faktor': ('Faktör modeli', lambda P, e, h: (factor_mu(P, e, h), kur_mu(P, e))),
}
if HAS_SF:
    METHODS['istatistik'] = ('İstatistik (ARIMA/ETS)', stat_mu)
if HAS_ARCH:
    METHODS['garch'] = ('GARCH oynaklık', lambda P, e, h: (0.25 * m(P['ro'], e - 252, e), kur_mu(P, e)))
VOL_GARCH = {'garch'}


def forecast(P, e, h, method='denge', sims=4000, seed=7, news=0.0, beta=None, bias=0.0, scale=1.0, ev_mult=None, path=False):
    """e: bilinen getiri sayısı (getiri 0..e-1 biliniyor). Log getiri dağılımı döndürür."""
    ro, rk, vo, vk = P['ro'], P['rk'], P['vo'], P['vk']
    m0 = max(30, e - 504)
    zo = ro[m0:e] / np.sqrt(vo[m0:e]); zk = rk[m0:e] / np.sqrt(vk[m0:e])
    zo = zo - zo.mean(); zk = zk - zk.mean()  # kalıntılardaki geçmiş trend simülasyona gizli eğilim taşımasın
    lo2, lk2 = np.mean(ro[m0:e] ** 2), np.mean(rk[m0:e] ** 2)
    if method in VOL_GARCH and HAS_ARCH:
        sO, sK = garch_sigma(P, e, h)
    else:
        f = PHI ** np.arange(h)
        sO = np.sqrt(lo2 + (vo[e] - lo2) * f); sK = np.sqrt(lk2 + (vk[e] - lk2) * f)
    if ev_mult is not None:
        sO = sO * ev_mult[:h]; sK = sK * np.sqrt(ev_mult[:h])
    muO, muK = METHODS[method][1](P, e, h)
    so = math.sqrt(vo[e]); b = 0.12 * so if beta is None else beta
    dN = news * b * np.exp(-np.arange(h) / 4)
    rng = np.random.default_rng(seed)
    j = rng.integers(0, len(zo), size=(sims, h))
    steps = muO + muK + dN + sO * zo[j] + sK * zk[j]
    res = steps.sum(1)
    c = np.median(res)
    res = c + (res - c) * scale + bias
    q = np.quantile(res, [0.1, 0.5, 0.9])
    out = {'lq': q.tolist(), 'med': math.expm1(q[1]), 'lo': math.expm1(q[0]), 'hi': math.expm1(q[2]),
           'pUp': float((res > 0).mean()), 'kurPart': math.expm1(muK * h), 'onsPart': math.expm1(muO * h)}
    if path:
        cum = np.cumsum(steps, 1)
        cm = np.median(cum, 0)
        cum = cm + (cum - cm) * scale + bias * (np.arange(1, h + 1) / h)
        out['fan'] = {k: np.expm1(np.quantile(cum, p, axis=0)).round(5).tolist() for k, p in (('p10', .1), ('p25', .25), ('p50', .5), ('p75', .75), ('p90', .9))}
    return out


def pinball(lq, y):
    return sum((p * (y - q) if y >= q else (1 - p) * (q - y)) for p, q in zip((0.1, 0.5, 0.9), lq)) / 3


def backtest(P, h, method):
    n = len(P['ons']); g = np.log(P['ons'] * P['kur'])
    hit = naive = cov = cnt = 0; loss = 0.0; u = []
    step = 5 if h >= 5 else 3
    for t in range(max(300, n - 1 - 504), n - h, step):
        f = forecast(P, t, h, method, sims=500, seed=t)
        y = g[t + h] - g[t]; lq = f['lq']
        hit += (lq[1] > 0) == (y > 0); naive += y > 0; cov += lq[0] <= y <= lq[2]
        u.append((y - lq[1]) / max(1e-9, lq[2] - lq[1]) if y >= lq[1] else (lq[1] - y) / max(1e-9, lq[1] - lq[0]))
        loss += pinball(lq, y); cnt += 1
    if not cnt:
        return None
    u.sort()
    raw = u[min(cnt - 1, int(0.8 * cnt))]; neff = cnt * step / h
    scale = max(0.7, min(2.0, 1 + (raw - 1) * neff / (neff + 10)))
    return {'hit': hit / cnt, 'naive': naive / cnt, 'cov': cov / cnt, 'loss': loss / cnt * 1e4, 'cnt': cnt, 'scale': scale}


def event_multiplier(P):
    """Fed karar günlerinden sonraki gün oynaklığın normalin kaç katı olduğunu geçmişten öğrenir."""
    idx = {d: i for i, d in enumerate(P['dates'])}
    z = []
    for d in FOMC_PAST:
        i = idx.get(d)
        if i is not None and i + 1 < len(P['ro']):
            for k in (i, i + 1):  # karar akşamı açıklanır; etki karar günü ve ertesi güne yayılır
                if k - 1 >= 0:
                    z.append((P['ro'][k - 1] ** 2) / P['vo'][k - 1])
    if len(z) < 10:
        return 1.3, len(z) // 2
    return float(np.clip(math.sqrt(np.mean(z)), 1.0, 2.5)), len(z) // 2


def future_days(last_date, h):
    d, out = dt.date.fromisoformat(last_date), []
    while len(out) < h:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out


def learn_news(P, news):
    idx = {d: i for i, d in enumerate(P['dates'])}
    xs, ys = [], []
    for d, h in news.get('days', {}).items():
        i = idx.get(d)
        if i is not None and i < len(P['ro']) and len(h['items']) >= 3:
            xs.append(h['score']); ys.append(P['ro'][i])
    n = len(xs); so = math.sqrt(P['vo'][-1])
    if n < 20:
        return {'n': n, 'beta': None}
    xs, ys = np.array(xs), np.array(ys)
    v = ((xs - xs.mean()) ** 2).sum()
    raw = ((xs - xs.mean()) * (ys - ys.mean())).sum() / v if v else 0
    beta = raw * n / (n + 40) + 0.12 * so * 40 / (n + 40)
    return {'n': n, 'beta': float(np.clip(beta, -0.5 * so, 0.5 * so))}


def update_log(L, P, price, fc, H):
    idx = {d: i for i, d in enumerate(P['dates'])}
    g = P['ons'] * P['kur'] / OZ
    for h, _ in HZ:
        if not any(x['d'] == TODAY and x['h'] == h for x in L):
            f = fc[str(h)]
            L.append({'d': TODAY, 'h': h, 'p': round(price, 2), 'med': round(f['med'], 5), 'lo': round(f['lo'], 5), 'hi': round(f['hi'], 5), 'm': f['method'], 'v': 3})
    dates = P['dates']
    for x in L:
        if x.get('act') is None:
            # tahmin günündeki son kapanıştan h işlem günü sonrası
            base = max((i for i, d in enumerate(dates) if d <= x['d']), default=None)
            if base is not None and base + x['h'] < len(dates) - 1:
                x['act'] = round(g[base + x['h']] / x['p'] - 1, 5)
                x['dAct'] = dates[base + x['h']]
    return L[-1500:]


def learn_bias(L, h):
    done = [x for x in L if x['h'] == h and x.get('act') is not None and x.get('v', 0) >= 3]
    if len(done) < 3:
        return {'n': len(done), 'bias': 0.0}
    done = done[-40:]  # piyasa değişir: son 40 sonuç, yenilere daha çok ağırlık
    w = np.array([0.95 ** (len(done) - 1 - i) for i in range(len(done))])
    b = float(np.sum(w * np.array([math.log1p(x['act']) - math.log1p(x['med']) for x in done])) / w.sum())
    return {'n': len(done), 'bias': b * len(done) / (len(done) + 8)}


# ---------------------------------------------------------------- 6. TELEGRAM
def tg(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    return get_json(f'https://api.telegram.org/bot{token}/{method}', data=data, timeout=30)


def fmt_tl(v, d=0):
    s = f'{v:,.{d}f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return s


def fmt_pct(v, d=1):
    return ('+' if v >= 0 else '−') + f'{abs(v) * 100:.{d}f}'.replace('.', ',') + '%'


def summary_text(S):
    p, fc = S['price'], S['forecast']
    lines = [f"<b>Gram altın: {fmt_tl(p, 2)} ₺</b> ({fmt_pct(S['chg1d'], 2)} dünden beri)",
             f"Ons {fmt_tl(S['ons'])} $ · Dolar {fmt_tl(S['kur'], 2)} ₺", '']
    for h, name in HZ:
        f = fc[str(h)]
        lines.append(f"<b>{name}:</b> {fmt_pct(f['med'])} · %{round(f['pUp'] * 100)} yukarı · aralık {fmt_tl(p * (1 + f['lo']))}–{fmt_tl(p * (1 + f['hi']))} ₺")
    ev = [e for e in S['events'] if e['date'] >= TODAY][:3]
    if ev:
        lines += ['', '<b>Yaklaşan:</b> ' + ' · '.join(f"{dt.date.fromisoformat(e['date']).strftime('%d.%m')} {e['title']}" for e in ev)]
    nw = S['news'][:2]
    if nw:
        lines += ['', '<b>Haber:</b>'] + [('🟢 ' if x['s'] > 0.1 else '🔴 ' if x['s'] < -0.1 else '⚪ ') + x['t'] for x in nw]
    if S.get('site'):
        lines += ['', S['site']]
    return '\n'.join(lines)


HELP = ('Komutlar:\n/durum — anlık fiyat ve tahminler\n/alarm 6500 — fiyat 6.500 ₺ seviyesine gelince haber ver\n'
        '/alarmlar — kurulu alarmlar\n/sil 6500 — alarmı sil (/sil hepsi)\n/yardim — bu mesaj\n\n'
        'Her sabah 09:00 civarı özet gelir; fiyat gün içinde %2\'den fazla oynarsa uyarı gelir. '
        'Komutlara cevap, bir sonraki kontrolde (en geç ~20 dk) gelir.')


def telegram(S, st):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        log('telegram: token yok, atlandı')
        return st
    fixed_chat = os.environ.get('TELEGRAM_CHAT_ID')
    if fixed_chat:
        st['chat'] = int(fixed_chat)
    price = S['price']

    def send(text):
        if st.get('chat'):
            try:
                tg(token, 'sendMessage', chat_id=st['chat'], text=text, parse_mode='HTML', disable_web_page_preview='true')
            except Exception as e:
                log('telegram gönderim hatası', e)
    try:
        upd = tg(token, 'getUpdates', offset=st.get('offset', 0) + 1, timeout=0)
        for u in upd.get('result', []):
            st['offset'] = u['update_id']
            msg = u.get('message') or u.get('edited_message') or {}
            chat = msg.get('chat', {}).get('id'); text = (msg.get('text') or '').strip()
            if not chat or not text:
                continue
            if not st.get('chat'):
                st['chat'] = chat  # ilk yazan kişi botun sahibi olur
                send('Merhaba! Bu bot artık yalnızca sana çalışıyor.\n\n' + HELP)
                continue
            if chat != st['chat']:
                continue
            cmd, *args = text.split()
            cmd = cmd.lower().split('@')[0]
            if cmd in ('/start', '/yardim', '/help'):
                send(HELP)
            elif cmd == '/durum':
                send(summary_text(S))
            elif cmd == '/alarm' and args:
                try:
                    v = tr_num(args[0])
                    st.setdefault('alerts', []).append({'v': v, 'yon': 'yukari' if v > price else 'asagi'})
                    send(f"Alarm kuruldu: gram {fmt_tl(v)} ₺ {'üstüne çıkınca' if v > price else 'altına inince'} haber vereceğim. (Şu an {fmt_tl(price)} ₺)")
                except Exception:
                    send('Örnek kullanım: /alarm 6500')
            elif cmd == '/alarmlar':
                al = st.get('alerts', [])
                send('Kurulu alarm yok.' if not al else 'Alarmlar:\n' + '\n'.join(f"• {fmt_tl(a['v'])} ₺ ({'yukarı' if a['yon'] == 'yukari' else 'aşağı'})" for a in al))
            elif cmd == '/sil':
                if args and args[0].lower() == 'hepsi':
                    st['alerts'] = []; send('Tüm alarmlar silindi.')
                elif args:
                    try:
                        v = tr_num(args[0]); before = len(st.get('alerts', []))
                        st['alerts'] = [a for a in st.get('alerts', []) if abs(a['v'] - v) > 0.5]
                        send('Silindi.' if len(st['alerts']) < before else 'Bu fiyatta alarm yok.')
                    except Exception:
                        send('Örnek: /sil 6500')
            else:
                send('Anlamadım. ' + HELP)
    except Exception as e:
        log('telegram getUpdates hatası', e)
    if not st.get('chat'):
        log('telegram: henüz kimse bota /start yazmadı')
        return st
    # fiyat alarmları
    keep = []
    for a in st.get('alerts', []):
        if (a['yon'] == 'yukari' and price >= a['v']) or (a['yon'] == 'asagi' and price <= a['v']):
            send(f"🔔 <b>Alarm:</b> gram altın {fmt_tl(price, 2)} ₺ oldu ({fmt_tl(a['v'])} ₺ hedefin {'üstünde' if a['yon'] == 'yukari' else 'altında'}).")
        else:
            keep.append(a)
    st['alerts'] = keep
    # sert hareket uyarısı (%2)
    ref = st.get('ref')
    if ref and abs(price / ref - 1) >= 0.02:
        send(f"⚡ Gram altın son bildirimden beri {fmt_pct(price / ref - 1)} hareket etti: {fmt_tl(price, 2)} ₺")
        st['ref'] = price
    # sabah özeti (TR saatiyle 09:00 sonrası, günde bir)
    now_tr = NOW.astimezone(TRT)
    if now_tr.hour >= 9 and st.get('lastSummary') != now_tr.date().isoformat():
        send('☀️ <b>Günaydın, altın özeti</b>\n\n' + summary_text(S))
        st['lastSummary'] = now_tr.date().isoformat(); st['ref'] = price
    if not st.get('ref'):
        st['ref'] = price
    return st


# ---------------------------------------------------------------- ANA AKIŞ
def offline_history():
    rng = np.random.default_rng(3)
    n = 1250; days = []
    d = dt.date.fromisoformat(TODAY)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d -= dt.timedelta(days=1)
    days.reverse()
    def walk(s, mu, sd):
        return (s * np.exp(np.cumsum(mu + sd * rng.standard_normal(n)))).round(4).tolist()
    return {'dates': days, 'ons': walk(1900, 0.0005, 0.01), 'kur': walk(19, 0.0011, 0.004), 'dxy': walk(103, 0, 0.004),
            'tnx': (4 + np.cumsum(0.04 * rng.standard_normal(n))).round(3).tolist(), 'silver': walk(23, 0.0004, 0.018), 'vix': (15 + 4 * np.abs(rng.standard_normal(n))).round(2).tolist()}, {}


def main():
    t0 = time.time()
    if OFFLINE:
        H, live = offline_history()
        kap = {'kaynak': 'test', 'fiyat': {}}
    else:
        H, live = fetch_history()
        kap = fetch_kapali()
    # en güncel anlık fiyatı son güne yaz
    if live.get('ons') and live.get('kur'):
        H['ons'][-1], H['kur'][-1] = live['ons'], live['kur']
    P = prep(H)
    e = len(P['ro'])
    ons, kur = P['ons'][-1], P['kur'][-1]
    gram_calc = ons * kur / OZ
    prem = None
    if kap and 'gram' in kap.get('fiyat', {}):
        prem = kap['fiyat']['gram']['sat'] / gram_calc - 1
        if not -0.05 < prem < 0.08:
            log('kuyumcu farkı makul değil, yok sayıldı', prem); prem = None
    src = {} if OFFLINE else collect_sources(kap)
    srcs = source_stats(src, load('sources.json', {}))
    ratio = antalya_ratio(src)
    base, base_note = 'hesap', 'ons × kur hesabı'
    if prem is not None:
        base, base_note = 'kapali', 'Kapalıçarşı satış'
    if ratio is not None and prem is not None:
        prem = (1 + prem) * ratio - 1
        base, base_note = 'akod', f'Antalya: Kapalıçarşı gram × AKOD ziynet farkı ({fmt_pct(ratio - 1, 2)})'
    price = gram_calc * (1 + (prem or 0))
    log('baz fiyat:', base_note, f'{price:.2f}')

    news = load('news.json', {}) if not OFFLINE else load('news.json', {'days': {}})
    if not OFFLINE:
        news = fetch_news(news)
    cal = fetch_calendar(load('calendar.json', {})) if not OFFLINE else {'events': [{'date': d, 'title': t, 'kind': k} for d, t, k in FIXED]}

    # yöntem seçimi (geriye dönük test ağır: günde bir kez yapılır, sonucu saklanır)
    bt, chosen = {}, {}
    cached = load('backtest.json', {})
    if cached.get('day') == TODAY and set(cached.get('methods', [])) == set(METHODS) and not OFFLINE:
        bt = cached['bt']; chosen = {int(h): k for h, k in cached['chosen'].items()}
        log('geriye dönük test bugünkü kayıttan alındı')
    for h, _ in ([] if bt else HZ):
        bt[str(h)] = {}
        for k in METHODS:
            try:
                bt[str(h)][k] = backtest(P, h, k)
            except Exception as ex:
                log('backtest hata', h, k, ex); bt[str(h)][k] = None
        ok = {k: v for k, v in bt[str(h)].items() if v}
        chosen[h] = min(ok, key=lambda k: ok[k]['loss']) if ok else 'denge'
    if not cached.get('day') == TODAY or OFFLINE:
        save('backtest.json', {'day': TODAY, 'methods': list(METHODS), 'bt': bt, 'chosen': {str(h): k for h, k in chosen.items()}})
    log(f'kütüphaneler: statsforecast={HAS_SF}, arch={HAS_ARCH}')
    log('seçilen yöntemler:', chosen, f'{time.time() - t0:.1f} sn')

    mult, n_ev = event_multiplier(P)
    fut = future_days(P['dates'][-1], 21)
    evd = {x['date'] for x in cal['events'] if x['kind'] in ('fed', 'tcmb', 'veri')}
    fed_next = {(dt.date.fromisoformat(x['date']) + dt.timedelta(days=1)).isoformat() for x in cal['events'] if x['kind'] == 'fed'}
    ev_mult = np.array([mult if (d in evd or d in fed_next) else 1.0 for d in fut])
    nl = learn_news(P, news)
    L = load('log.json', [])
    fc = {}
    for h, name in HZ:
        lb = learn_bias(L, h)
        best = bt[str(h)].get(chosen[h]) or {}
        f = forecast(P, e, h, chosen[h], news=news.get('score', 0), beta=nl['beta'], bias=lb['bias'], scale=best.get('scale', 1), ev_mult=ev_mult, path=(h == 21))
        f.update({'method': chosen[h], 'methodName': METHODS[chosen[h]][0], 'name': name, 'scale': best.get('scale', 1), 'biasN': lb['n'], 'bias': lb['bias'],
                  'events': [x for x in cal['events'] if fut[0] <= x['date'] <= fut[h - 1]]})
        f.pop('lq', None)
        fc[str(h)] = f
    L = update_log(L, P, gram_calc, fc, H)

    chg1d = (P['ons'][-1] * P['kur'][-1]) / (P['ons'][-2] * P['kur'][-2]) - 1
    out = {'time': NOW.isoformat(timespec='minutes'), 'date': P['dates'][-1], 'price': price, 'gramCalc': gram_calc, 'premium': prem, 'ons': ons, 'kur': kur,
           'chg1d': chg1d, 'forecast': fc, 'futureDays': fut,
           'backtest': {h: {k: v for k, v in d.items()} for h, d in bt.items()},
           'learn': {'news': nl, 'eventMult': mult, 'eventN': n_ev, 'bias': {str(h): learn_bias(L, h) for h, _ in HZ}},
           'kapali': kap, 'base': base, 'baseNote': base_note, 'antalyaRatio': ratio, 'report': build_report(L, P), 'methods': {k: v[0] for k, v in METHODS.items()}, 'logs': LOGS[-40:]}
    save('history.json', {k: v if k == 'dates' else [None if x is None else round(float(x), 4) for x in v] for k, v in H.items()})
    save('forecast.json', out)
    save('news.json', news)
    save('calendar.json', cal)
    save('log.json', L)
    save('sources.json', srcs)
    st = load('telegram.json', {})
    S = {'price': price, 'ons': ons, 'kur': kur, 'chg1d': chg1d, 'forecast': fc, 'events': cal['events'], 'news': news.get('latest', []),
         'site': os.environ.get('SITE_URL', '')}
    if not OFFLINE:
        st = telegram(S, st)
    save('telegram.json', st)
    log(f'bitti: gram {price:.2f} ({time.time() - t0:.1f} sn)')


if __name__ == '__main__':
    main()
