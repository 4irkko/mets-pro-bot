"""METS -> draft Telegram post with photos and optional evidence-based market asking prices."""
import asyncio, io, logging, os, re, sqlite3, time
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image
from playwright.async_api import async_playwright
from telegram import Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters


load_dotenv()
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('mets')
TOKEN=os.getenv('BOT_TOKEN','')
ALLOWED={int(x) for x in os.getenv('ALLOWED_USER_IDS','').split(',') if x.strip().isdigit()}
MAX_IMAGES=max(1,min(30,int(os.getenv('MAX_IMAGES','20'))))
URL_RE=re.compile(r'https?://(?:www\.)?m-ets\.ru/\d+(?:-\d+)?/?(?:\?[^\s]*)?',re.I)
YEAR_RE=re.compile(r'\b(19[89]\d|20[012]\d)\b')
VIN_RE=re.compile(r'\b[A-HJ-NPR-Z0-9]{17}\b')
PRICE_RE=re.compile(r'(?<!\d)(\d[\d\s\u00a0]{4,12})\s*(?:₽|руб(?:лей|ля)?|р\.)',re.I)
BRANDS={'Mercedes-Benz':['mercedes-benz','мерседес','mercedes'], 'BMW':['bmw','бмв'], 'Lexus':['lexus','лексус'], 'Toyota':['toyota','тойота'], 'Volkswagen':['volkswagen','фольксваген'], 'Audi':['audi','ауди'], 'Kia':['kia','киа'], 'Hyundai':['hyundai','хендай','хёндэ'], 'Nissan':['nissan','ниссан'], 'Ford':['ford','форд'], 'Skoda':['skoda','шкода'], 'Renault':['renault','рено'], 'Mazda':['mazda','мазда'], 'Honda':['honda','хонда'], 'Lada':['lada','лада','ваз'], 'Porsche':['porsche','порше'], 'Land Rover':['land rover','ленд ровер']}
DB=Path('data/history.sqlite3')

@dataclass
class Lot:
    url:str
    title:str
    text:str
    images:list[bytes]=field(default_factory=list)
    status:int=0
    error:str=''


def db_init():
    DB.parent.mkdir(exist_ok=True,parents=True)
    with sqlite3.connect(DB) as db:
        db.execute('CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts INTEGER, user_id INTEGER, url TEXT, status TEXT, images INTEGER)')

def record(uid,url,status,count):
    with sqlite3.connect(DB) as db:
        db.execute('INSERT INTO runs(ts,user_id,url,status,images) VALUES(?,?,?,?,?)',(int(time.time()),uid,url,status,count))

def valid(url):
    p=urlparse(url)
    return p.scheme=='https' and p.hostname in ('m-ets.ru','www.m-ets.ru') and bool(re.fullmatch(r'/\d+(?:-\d+)?/?',p.path))

def tidy(html):
    soup=BeautifulSoup(html,'html.parser')
    for el in soup(['script','style','noscript','nav','footer','header']): el.decompose()
    return '\n'.join(x for x in (re.sub(r'\s+',' ',l).strip() for l in soup.get_text('\n').splitlines()) if x)

def image_jpeg(data):
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width<300 or im.height<200: return None
            im.thumbnail((2200,2200))
            if im.mode!='RGB': im=im.convert('RGB')
            buf=io.BytesIO(); im.save(buf,format='JPEG',quality=85,optimize=True)
            return buf.getvalue() if buf.tell()<9_500_000 else None
    except Exception: return None

async def scrape(url):
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=os.getenv('HEADLESS','true').lower()!='false')
        ctx=await browser.new_context(locale='ru-RU',viewport={'width':1400,'height':1000})
        page=await ctx.new_page()
        raw=[]
        async def capture(resp):
            try:
                if resp.status!=200 or not resp.headers.get('content-type','').lower().startswith('image/'): return
                body=await resp.body()
                if 12_000<len(body)<12_000_000: raw.append(body)
            except Exception: pass
        page.on('response',capture)
        try:
            resp=await page.goto(url,wait_until='domcontentloaded',timeout=45000)
            status=resp.status if resp else 0
            await page.wait_for_timeout(2500)
            for frac in (.35,.7,1):
                await page.evaluate('(f)=>window.scrollTo(0,document.body.scrollHeight*f)',frac)
                await page.wait_for_timeout(850)
            # Attempt image links exposed by the rendered DOM, including lazy-loaded galleries.
            srcs=await page.locator('img').evaluate_all('els=>els.flatMap(e=>[e.currentSrc,e.src,e.getAttribute("data-src"),e.getAttribute("data-original"),e.getAttribute("data-lazy-src")]).filter(Boolean)')
            srcs+=await page.locator('a[href]').evaluate_all(r'els=>els.map(e=>e.href).filter(x=>/\.(jpg|jpeg|png|webp)(\?|$)/i.test(x))')
            for src in dict.fromkeys(srcs):
                if len(raw)>=80: break
                target=urljoin(url,src)
                purl=urlparse(target)
                if purl.scheme!='https' or not purl.hostname or not (purl.hostname=='m-ets.ru' or purl.hostname.endswith('.m-ets.ru')): continue
                try:
                    response=await ctx.request.get(target,timeout=10000)
                    if response.ok and response.headers.get('content-type','').startswith('image/'):
                        raw.append(await response.body())
                except Exception: pass
            html=await page.content()
            result=Lot(url,await page.title(),tidy(html),status=status)
            if status in (401,403,429): result.error=f'МЭТС ответил HTTP {status}; доступ к данным ограничен.'
            seen=set()
            for body in raw:
                jpg=image_jpeg(body)
                if jpg and hash(jpg) not in seen:
                    seen.add(hash(jpg)); result.images.append(jpg)
                    if len(result.images)>=MAX_IMAGES: break
            return result
        except Exception as e:
            return Lot(url,'','',error=f'{type(e).__name__}: {str(e)[:160]}')
        finally:
            await browser.close()

def field(text,patterns):
    for pattern in patterns:
        m=re.search(pattern,text,re.I)
        if m: return re.sub(r'\s+',' ',m.group(1)).strip(' .,:;')[:120]
    return None

def extract(lot):
    text=lot.text
    vin=(VIN_RE.search(text) or VIN_RE.search(lot.title))
    year=YEAR_RE.search(lot.title+' '+text[:3500])
    make=None; model=None
    hay=(lot.title+' '+text[:2000]).lower()
    for brand,variants in BRANDS.items():
        for v in variants:
            match=re.search(r'(?<!\w)'+re.escape(v)+r'(?!\w)',hay,re.I)
            if match:
                make=brand
                after=hay[match.end():match.end()+50]
                mm=re.match(r'\s*([a-zа-я0-9][a-zа-я0-9-]{0,16})',after,re.I)
                if mm and mm.group(1) not in ('автомобиль','легковой','год','года','тип'): model=mm.group(1).upper()
                break
        if make: break
    return {
        'vin':vin.group(0) if vin else None,
        'year':int(year.group(1)) if year else None,
        'make':make,'model':model,
        'mileage':field(text,[r'(?:пробег|одометр)\s*[:–-]?\s*([\d\s]{2,12}\s*(?:км|тыс\.?\s*км))']),
        'location':field(text,[r'(?:местонахождение|место нахождения|адрес имущества)\s*[:–-]?\s*([^\n]{4,110})']),
        'deposit':field(text,[r'(?:размер задатка|задаток)\s*[:–-]?\s*([^\n]{2,70})']),
        'starting_price':field(text,[r'(?:начальная цена(?: продажи)?|начальная стоимость)\s*[:–-]?\s*([^\n]{3,70})']),
    }

def schedule_lines(text):
    lines=text.splitlines(); out=[]
    date=re.compile(r'\b\d{2}[./]\d{2}(?:[./]\d{4})?\b')
    for i,line in enumerate(lines):
        if date.search(line) and (PRICE_RE.search(line) or (i+1<len(lines) and PRICE_RE.search(lines[i+1]))):
            s=line+((' — '+lines[i+1]) if not PRICE_RE.search(line) else '')
            if s not in out: out.append(s[:190])
        if len(out)>=20: break
    return out

def relevant(text):
    keys=('состояни','поврежден','неисправ','ремонт','обременен','залог','двигател','дтп','осмотр','птс')
    lines=[l for l in text.splitlines() if any(k in l.lower() for k in keys)]
    return lines[:7]

def format_post(lot,data,market,reason):
    title=' '.join(str(x) for x in (data['make'],data['model'],data['year']) if x) or (lot.title[:120] if lot.title else 'Лот МЭТС')
    out=[f'📦 <b>{escape(title)}</b>', '',f'🔗 <a href="{escape(lot.url,quote=True)}">Карточка торгов МЭТС</a>']
    for label,key in [('VIN','vin'),('Пробег','mileage'),('Местонахождение','location'),('Начальная цена','starting_price'),('Задаток','deposit')]:
        if data[key]: out.append(f'<b>{label}:</b> {escape(str(data[key]))}')
    dates=schedule_lines(lot.text)
    if dates:
        out.extend(['','📉 <b>Найденные строки графика снижения (проверьте по первоисточнику):</b>'])
        out.extend(escape(x) for x in dates[:12])
    else: out.extend(['','📉 График снижения автоматически не подтверждён.'])
    if market:
        out.extend(['',f'💰 <b>Ориентир цен объявлений:</b> {market["low"]:,}–{market["high"]:,} ₽'.replace(',',' '),f'📊 Медиана {market["median"]:,} ₽; аналогов: {len(market["comparables"])}'.replace(',',' '),'⚠️ Это цены объявлений, не подтверждённые сделки. Не учтено состояние лота.'])
        # Comparables omitted from public-facing post
    else: out.extend(['','💰 <b>Рыночная оценка не подтверждена.</b>',escape(reason or 'Нет проверенных аналогов.')])
    notes=relevant(lot.text)
    if notes: out.extend(['','🔎 <b>Фрагменты описания, требующие проверки:</b>']+[escape(x[:220]) for x in notes])
    out.extend(['','⚠️ Проверяйте документацию, состояние, ограничения и сроки подачи заявки непосредственно на площадке.'])
    result='\n'.join(out)
    return result if len(result)<=3900 else result[:3500]+'\n\n⚠️ Полные сведения смотрите в карточке торгов.'

