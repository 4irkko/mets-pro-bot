"""METS Telegram editorial bot. Best-effort extraction; never fabricates prices."""
import asyncio, html, io, json, logging, os, re, sqlite3, time
from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as K, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from scraper import scrape, extract, format_post
from market import estimate

logging.basicConfig(level=logging.INFO)
log=logging.getLogger('mets-universal')
TOKEN=os.getenv('BOT_TOKEN','')
OWNER_IDS={int(s) for s in os.getenv('OWNER_IDS','').split(',') if s.strip().isdigit()}
DB=Path(os.getenv('DB_PATH','data/mets.sqlite3'))
INTERVAL=max(300,int(os.getenv('MONITOR_INTERVAL_SECONDS','1800')))
URL=re.compile(r'https://(?:www\.)?m-ets\.ru/\d+(?:-\d+)?(?:/|\b)',re.I)
PHOTO_DIR=DB.parent/'photos'

def conn():
    DB.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(DB,timeout=20)
    c.row_factory=sqlite3.Row
    return c

def setup():
    with conn() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS settings(uid INTEGER PRIMARY KEY,footer TEXT DEFAULT '',channel TEXT DEFAULT '',edit_id INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS drafts(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER,url TEXT,text TEXT,created INTEGER,status TEXT DEFAULT 'draft',channel TEXT DEFAULT '',message_id INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS seen(url TEXT PRIMARY KEY,first_seen INTEGER);
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);''')
    PHOTO_DIR.mkdir(exist_ok=True)

def owner(u): return bool(u and (not OWNER_IDS or u.id in OWNER_IDS))
def settings(uid):
    with conn() as c:
        c.execute('INSERT OR IGNORE INTO settings(uid) VALUES(?)',(uid,))
        return c.execute('SELECT * FROM settings WHERE uid=?',(uid,)).fetchone()
def keyboard(i):
    return K([[B('✏️ Редактировать',callback_data=f'edit:{i}'),B('📢 Опубликовать',callback_data=f'pub:{i}')],[B('🗑 Удалить',callback_data=f'del:{i}')]])
def main_menu():
    return K([[B('📝 Черновики',callback_data='drafts'),B('📢 Канал',callback_data='channel')],[B('🧾 Подвал',callback_data='footer'),B('📡 Мониторинг',callback_data='monitor')]])
def append_footer(text,footer):
    result=text.strip() + ('\n\n'+footer.strip() if footer.strip() else '')
    return result[:4000]
def photos(i): return sorted(PHOTO_DIR.glob(f'{i}_*.jpg'))

async def start(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user): return
    settings(update.effective_user.id)
    await update.message.reply_text('МЭТС | Редактор торгов\nПришлите ссылку на лот. /channel @имя_канала — привязать канал, /footer текст — задать подвал, /monitor_on — включить мониторинг, /monitor_off — выключить.\nДля приватного канала: /channel -100...\nВаш ID: '+str(update.effective_user.id),reply_markup=main_menu())
async def channel(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user): return
    uid=update.effective_user.id
    if not ctx.args: await update.message.reply_text('Укажите /channel @канал или /channel -1001234567890. Сначала добавьте бота администратором с правом публикации.');return
    ch=ctx.args[0].strip()
    try:
        chat=await ctx.bot.get_chat(ch)
        member=await ctx.bot.get_chat_member(chat.id,ctx.bot.id)
        if chat.type!='channel' or member.status not in ('administrator','creator') or not getattr(member,'can_post_messages',False):
            raise ValueError('Бот не является администратором канала с правом публикации')
        with conn() as c:c.execute('UPDATE settings SET channel=? WHERE uid=?',(str(chat.id),uid))
        await update.message.reply_text(f'✅ Канал подключён: {chat.title}')
    except Exception as e: await update.message.reply_text('❌ Не удалось подключить канал: '+str(e)[:250])
async def footer(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user):return
    uid=update.effective_user.id
    value=' '.join(ctx.args)
    if not value:
        await update.message.reply_text('Текущий подвал:\n'+(settings(uid)['footer'] or '(пусто)')+'\n\nДля изменения: /footer ваш текст\nДля очистки: /footer_clear')
        return
    with conn() as c:c.execute('UPDATE settings SET footer=? WHERE uid=?',(value[:1200],uid))
    await update.message.reply_text('✅ Подвал сохранён. Добавится к новым публикациям.')
async def footer_clear(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user):return
    with conn() as c:c.execute('UPDATE settings SET footer="" WHERE uid=?',(update.effective_user.id,))
    await update.message.reply_text('Подвал очищен.')
async def monitor_set(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user):return
    enabled=update.message.text.split()[0]=='/monitor_on'
    with conn() as c:c.execute('INSERT OR REPLACE INTO state(key,value) VALUES(?,?)',(f'monitor:{update.effective_user.id}',str(int(enabled))))
    await update.message.reply_text('📡 Мониторинг '+('включён' if enabled else 'выключен')+f'. Интервал {INTERVAL//60} минут. Новые торги приходят ссылками; полная карточка может быть недоступна.')
async def create_draft(uid,url,ctx,notify=True):
    lot=await asyncio.wait_for(scrape(url),timeout=110)
    if lot.error or len(lot.text)<100: raise RuntimeError(lot.error or 'Карточка недоступна или не содержит данных')
    data=extract(lot)
    market=None;reason='Нет подтверждённых аналогов'
    if data.get('make') and data.get('model') and data.get('year'):
        try:market,reason=await asyncio.wait_for(estimate(data['make'],data['model'],data['year']),timeout=35)
        except Exception as e:log.warning('market: %s',e)
    post=format_post(lot,data,market,reason)
    with conn() as c:
        cur=c.execute('INSERT INTO drafts(uid,url,text,created) VALUES(?,?,?,?)',(uid,url,post,int(time.time())))
        did=cur.lastrowid
    for n,blob in enumerate(lot.images[:10]):
        (PHOTO_DIR/f'{did}_{n:02}.jpg').write_bytes(blob)
    if notify: await show_draft(ctx.bot,uid,did)
    return did
async def show_draft(bot,uid,did):
    with conn() as c:r=c.execute('SELECT * FROM drafts WHERE id=? AND uid=?',(did,uid)).fetchone()
    if not r:return
    await bot.send_message(uid,append_footer(r['text'],settings(uid)['footer']),parse_mode='HTML',disable_web_page_preview=True,reply_markup=keyboard(did))
    if photos(did):
        try:
            files=[open(p,'rb') for p in photos(did)[:10]]
            try:await bot.send_media_group(uid,[InputMediaPhoto(f) for f in files])
            finally:
                for f in files:f.close()
        except Exception as e:log.warning('preview photos %s: %s',did,e)
async def handle(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user):return
    uid=update.effective_user.id
    settings(uid)
    msg=update.message.text or ''
    state=settings(uid)['edit_id']
    if state:
        with conn() as c:
            row=c.execute('SELECT id FROM drafts WHERE id=? AND uid=? AND status="draft"',(state,uid)).fetchone()
            c.execute('UPDATE settings SET edit_id=0 WHERE uid=?',(uid,))
            if row:c.execute('UPDATE drafts SET text=? WHERE id=?',(html.escape(msg[:3800]),state))
        if row:await update.message.reply_text('✅ Текст заменён. Подвал добавится автоматически.');await show_draft(ctx.bot,uid,state)
        else:await update.message.reply_text('Черновик уже недоступен.')
        return
    m=URL.search(msg)
    if not m:await update.message.reply_text('Пришлите ссылку на лот МЭТС или используйте /start.');return
    url=m.group(0).rstrip('/')
    await update.message.reply_text('⏳ Получаю данные. Это может занять до двух минут.')
    try:await create_draft(uid,url,ctx)
    except Exception as e:log.exception('scrape');await update.message.reply_text('❌ Не удалось создать пост: '+str(e)[:300]+'\nПопробуйте позднее; выдумывать характеристики не буду.')
async def buttons(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not owner(q.from_user):await q.answer('Нет доступа',show_alert=True);return
    uid=q.from_user.id
    await q.answer()
    act,_,arg=q.data.partition(':')
    if act=='footer':await q.message.reply_text('Используйте /footer ваш текст');return
    if act=='channel':await q.message.reply_text('Используйте /channel @канал');return
    if act=='monitor':await q.message.reply_text('Команды: /monitor_on и /monitor_off');return
    if act=='drafts':
        with conn() as c:rows=c.execute('SELECT id,url FROM drafts WHERE uid=? AND status="draft" ORDER BY id DESC LIMIT 10',(uid,)).fetchall()
        await q.message.reply_text('Черновики:\n'+('\n'.join(f'#{r["id"]} {r["url"]}' for r in rows) if rows else 'Нет черновиков')+'\nОткрыть: /draft НОМЕР');return
    try:did=int(arg)
    except ValueError:return
    with conn() as c:r=c.execute('SELECT * FROM drafts WHERE id=? AND uid=?',(did,uid)).fetchone()
    if not r:await q.message.reply_text('Черновик не найден');return
    if act=='edit':
        if r['status']!='draft':await q.message.reply_text('Этот пост уже опубликован.');return
        with conn() as c:c.execute('UPDATE settings SET edit_id=? WHERE uid=?',(did,uid))
        await q.message.reply_text('Отправьте НОВЫЙ полный текст поста одним сообщением. HTML-разметка будет преобразована в обычный текст. Для отмены: /cancel')
    elif act=='del':
        with conn() as c:c.execute('UPDATE drafts SET status="deleted" WHERE id=? AND uid=? AND status="draft"',(did,uid))
        await q.message.reply_text('Черновик удалён.')
    elif act=='pub':
        if r['status']!='draft':await q.message.reply_text('Этот пост уже обработан.');return
        ch=settings(uid)['channel']
        if not ch:await q.message.reply_text('Сначала подключите канал: /channel @имя');return
        # Prevent double click: reserve before network call, restore on failure.
        with conn() as c:
            updated=c.execute('UPDATE drafts SET status="sending" WHERE id=? AND uid=? AND status="draft"',(did,uid)).rowcount
        if not updated:await q.message.reply_text('Публикация уже обрабатывается.');return
        try:
            text=append_footer(r['text'],settings(uid)['footer'])
            # Publish text first: no Telegram caption truncation; then photos as separate album.
            sent=await ctx.bot.send_message(ch,text,parse_mode='HTML',disable_web_page_preview=True)
            with conn() as c:c.execute('UPDATE drafts SET status="published",channel=?,message_id=? WHERE id=?',(ch,sent.message_id,did))
            paths=photos(did)
            if paths:
                files=[open(p,'rb') for p in paths[:10]]
                try:await ctx.bot.send_media_group(ch,[InputMediaPhoto(f) for f in files])
                except Exception as e:log.exception('published but photos failed');await q.message.reply_text('⚠️ Текст опубликован, но фото не отправились: '+str(e)[:140])
                finally:
                    for f in files:f.close()
            await q.message.reply_text('✅ Пост опубликован. Повторная отправка заблокирована.')
        except Exception as e:
            log.exception('publish failed')
            with conn() as c:c.execute('UPDATE drafts SET status="draft" WHERE id=? AND status="sending"',(did,))
            await q.message.reply_text('❌ Публикация не подтверждена: '+str(e)[:220]+'\nПроверьте канал перед повторной попыткой.')
async def cancel(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user):return
    with conn() as c:c.execute('UPDATE settings SET edit_id=0 WHERE uid=?',(update.effective_user.id,))
    await update.message.reply_text('Редактирование отменено.')
async def draft(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user):return
    try:did=int(ctx.args[0])
    except (IndexError,ValueError):await update.message.reply_text('Формат: /draft 123');return
    await show_draft(ctx.bot,update.effective_user.id,did)

async def fetch_feed():
    # Collect only explicit lot links. The homepage may contain navigation or stale records.
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        try:
            page=await browser.new_page(locale='ru-RU')
            resp=await page.goto('https://m-ets.ru/',wait_until='domcontentloaded',timeout=45000)
            if not resp or resp.status>=400:raise RuntimeError(f'Homepage HTTP {resp.status if resp else "unknown"}')
            await page.wait_for_timeout(1200)
            anchors=await page.locator('a[href]').evaluate_all('els=>els.map(a=>({href:a.href,text:a.innerText}))')
            found=[]
            for a in anchors:
                u=a['href'].split('?')[0].rstrip('/')
                if URL.fullmatch(u) and u not in found:found.append(u)
            if not found:raise RuntimeError('No lot links found; website layout may have changed')
            return found[:150]
        finally:await browser.close()

async def monitor_loop(app):
    await asyncio.sleep(15)
    while True:
        try:
            with conn() as c:users=[int(r['key'].split(':')[1]) for r in c.execute("SELECT key FROM state WHERE key LIKE 'monitor:%' AND value='1'")]
            if users:
                urls=await fetch_feed()
                with conn() as c:
                    seeded=c.execute("SELECT value FROM state WHERE key='feed_seeded'").fetchone()
                    new=[]
                    for u in urls:
                        if c.execute('INSERT OR IGNORE INTO seen(url,first_seen) VALUES(?,?)',(u,int(time.time()))).rowcount:new.append(u)
                    if not seeded:c.execute("INSERT INTO state(key,value) VALUES('feed_seeded','1')")
                if seeded:
                    for uid in users:
                        for u in reversed(new[:25]):
                            try:await app.bot.send_message(uid,'🆕 Обнаружена ссылка на торги МЭТС:\n'+u+'\nОтправьте ссылку боту для создания поста.')
                            except Exception as e:log.warning('notify uid=%s: %s',uid,e)
                log.info('feed links=%d new=%d seed=%s',len(urls),len(new),not bool(seeded))
        except Exception as e:log.exception('monitor failed: %s',e)
        await asyncio.sleep(INTERVAL)
async def post_init(app):app.bot_data['monitor_task']=asyncio.create_task(monitor_loop(app))
async def post_shutdown(app):
    task=app.bot_data.get('monitor_task')
    if task:task.cancel()
def main():
    if not TOKEN:raise SystemExit('BOT_TOKEN missing')
    if not OWNER_IDS:raise SystemExit('OWNER_IDS required for security; get your ID from @userinfobot')
    setup()
    app=Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).concurrent_updates(False).build()
    for name,func in [('start',start),('channel',channel),('footer',footer),('footer_clear',footer_clear),('monitor_on',monitor_set),('monitor_off',monitor_set),('cancel',cancel),('draft',draft)]:app.add_handler(CommandHandler(name,func))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle))
    app.run_polling(drop_pending_updates=False)
if __name__=='__main__':main()
