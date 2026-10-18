"""Free best-effort public search for asking-price ranges. No API key required."""
import re
import statistics
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlparse, urlencode
import httpx
from bs4 import BeautifulSoup

PRICE_RE = re.compile(r'(?<!\d)(\d[\d\s\u00a0]{4,12})\s*(?:₽|руб(?:лей|ля)?|р\.)', re.I)
YEAR_RE = re.compile(r'\b(?:19[89]\d|20[012]\d)\b')
SITES = ('auto.ru', 'drom.ru', 'avito.ru')

@dataclass
class Comparable:
    price: int
    url: str
    title: str

def extract_price(text):
    values = []
    for match in PRICE_RE.finditer(text):
        value = int(re.sub(r'\D', '', match.group(1)))
        if 100_000 <= value <= 100_000_000:
            values.append(value)
    return values

def candidate_ok(title, snippet, make, model, year):
    text = (title + ' ' + snippet).lower().replace('ё', 'е')
    if not all(str(x).lower().replace('ё', 'е') in text for x in (make, model)):
        return False
    years = [int(y) for y in YEAR_RE.findall(text)]
    return any(abs(y - year) <= 2 for y in years)

async def _bing_rss(client, query):
    url = 'https://www.bing.com/search?' + urlencode({'q': query, 'format': 'rss'})
    response = await client.get(url)
    response.raise_for_status()
    root = ET.fromstring(response.text)
    return [{'title': item.findtext('title') or '', 'snippet': BeautifulSoup(item.findtext('description') or '', 'html.parser').get_text(' ', strip=True), 'url': item.findtext('link') or ''} for item in root.findall('.//item')]

async def _duckduckgo_html(client, query):
    response = await client.get('https://html.duckduckgo.com/html/', params={'q': query})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    rows = []
    for block in soup.select('.result'):
        link = block.select_one('.result__a')
        if not link:
            continue
        snippet = block.select_one('.result__snippet')
        href = link.get('href', '')
        # DDG may wrap the destination in a redirect URL.
        from urllib.parse import parse_qs, unquote
        if 'duckduckgo.com' in urlparse(href).netloc:
            href = unquote(parse_qs(urlparse(href).query).get('uddg', [''])[0])
        rows.append({'title': link.get_text(' ', strip=True), 'snippet': snippet.get_text(' ', strip=True) if snippet else '', 'url': href})
    return rows

async def estimate(make, model, year):
    if not (make and model and year):
        return None, 'Недостаточно характеристик для поиска аналогов.'
    query = f'{make} {model} {year} купить цена руб site:auto.ru OR site:drom.ru OR site:avito.ru'
    results = []
    errors = []
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; METSMarketResearch/1.0)'}
    async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
        for search in (_bing_rss, _duckduckgo_html):
            try:
                results.extend(await search(client, query))
            except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
                errors.append(type(exc).__name__)
    comps = []
    seen = set()
    for row in results:
        url = row['url']
        host = (urlparse(url).hostname or '').lower()
        if not any(host == site or host.endswith('.' + site) for site in SITES):
            continue
        if url in seen or not candidate_ok(row['title'], row['snippet'], make, model, year):
            continue
        prices = set(extract_price(row['title'] + ' ' + row['snippet']))
        if len(prices) != 1:
            continue
        seen.add(url)
        comps.append(Comparable(prices.pop(), url, row['title']))
    minimum = 3
    if len(comps) < minimum:
        return None, f'Недостаточно подтверждаемых цен аналогов ({len(comps)} из {minimum}). Бесплатный поиск может ограничивать доступ.'
    med = statistics.median(c.price for c in comps)
    filtered = [c for c in comps if 0.6 * med <= c.price <= 1.6 * med]
    if len(filtered) < minimum:
        return None, 'Слишком большой разброс найденных цен.'
    prices = sorted(c.price for c in filtered)
    lo = prices[int((len(prices) - 1) * 0.2)]
    hi = prices[int((len(prices) - 1) * 0.8)]
    return {'low': lo, 'high': hi, 'median': int(statistics.median(prices)), 'comparables': filtered}, None
