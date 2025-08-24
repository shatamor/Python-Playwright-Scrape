import discord
import os
import requests
import json
from flask import Flask
from threading import Thread
from playwright.async_api import async_playwright
import asyncio
import time
import re # İsim temizleme fonksiyonu için Regex kütüphanesini ekledik

# --- Global Değişkenler ---
playwright = None
browser = None
currency_cache = { "rate": None, "last_fetched": 0 }

# --- Web Sunucusu ve Keep Alive ---
# Render için bu kısım gerekli değil ama bir zararı da yok.
app = Flask('')
@app.route('/')
def home(): return "Bot Aktif ve Çalışıyor!"
def run(): app.run(host='0.0.0.0', port=8080)
def keep_alive():
    t = Thread(target=run)
    t.start()

# --- YENİ: Oyun Adı Temizleme Fonksiyonu ---
def clean_game_name(game_name):
    # ™, ®, ©, :, -, ' gibi özel karakterleri kaldırır.
    # Ve metni küçük harfe çevirir.
    cleaned_name = re.sub(r"[^\w\s]", "", game_name, flags=re.UNICODE)
    return cleaned_name.strip().lower()

# --- Döviz Kuru Alma Fonksiyonu ---
def get_usd_to_try_rate():
    global currency_cache
    if time.time() - currency_cache["last_fetched"] > 3600:
        try:
            response = requests.get("https://api.frankfurter.app/latest?from=USD&to=TRY")
            if response.status_code == 200:
                rate = response.json().get("rates", {}).get("TRY")
                if rate:
                    currency_cache["rate"] = rate
                    currency_cache["last_fetched"] = time.time()
                    return rate
            return currency_cache["rate"]
        except Exception: return currency_cache["rate"]
    else: return currency_cache["rate"]

# --- Steam Fiyat Alma Fonksiyonu ---
def get_steam_price(game_name):
    try:
        search_url = f"https://store.steampowered.com/api/storesearch/?term={requests.utils.quote(game_name)}&l=turkish&cc=TR"
        response = requests.get(search_url)
        if response.status_code != 200 or not response.json().get('items'): return None
        search_results = response.json().get('items', [])
        best_match = search_results[0] if search_results else None
        if not best_match: return "Oyun bulunamadı."
        app_id = best_match.get('id')
        app_details_url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=us"
        response_details = requests.get(app_details_url)
        if response_details.status_code != 200: return "Hata: Detaylar alınamadı."
        data = response_details.json()
        if not data or str(app_id) not in data or not data[str(app_id)].get('success'): return "Geçerli veri alınamadı."
        game_data = data[str(app_id)]['data']
        if game_data.get('is_free', False): return "Ücretsiz!"
        if 'price_overview' in game_data:
            price_in_cents = game_data['price_overview']['final']
            price_float = price_in_cents / 100.0
            currency = game_data['price_overview']['currency']
            return (price_float, currency)
        else: return "Fiyat bilgisi yok."
    except Exception as e: return "Hata oluştu."

# --- Epic Games Link Bulma Fonksiyonu ---
def get_epic_games_link(game_name):
    query = requests.utils.quote(game_name)
    return f"https://store.epicgames.com/tr/browse?q={query}&sortBy=relevancy&sortDir=DESC"

# --- PlayStation Store Fiyat Kazıma Fonksiyonu ---
async def get_playstation_price(game_name):
    global browser
    if not browser or not browser.is_connected(): return "Tarayıcı hazır değil."
    page = None
    try:
        page = await browser.new_page()
        page.set_default_timeout(90000)
        search_url = f"https://store.playstation.com/tr-tr/search/{requests.utils.quote(game_name)}"
        await page.goto(search_url)
        first_result_selector = 'div[data-qa^="search#productTile"] a.psw-link'
        first_result = page.locator(first_result_selector).first
        if await first_result.count() == 0: await page.close(); return "Oyun bulunamadı."
        await first_result.click()
        final_price_selector = 'span[data-qa="mfeCtaMain#offer0#finalPrice"]'
        await page.wait_for_selector(final_price_selector)
        final_price_element = page.locator(final_price_selector).first
        final_price_text = await final_price_element.inner_text()
        if "Dahil" in final_price_text:
            original_price_selector = 'span[data-qa="mfeCtaMain#offer0#originalPrice"]'
            original_price_element = page.locator(original_price_selector).first
            if await original_price_element.count() > 0:
                original_price_text = await original_price_element.inner_text()
                await page.close()
                return f"{original_price_text}\n*PS Plus'a Dahil*"
            else: await page.close(); return final_price_text
        else: await page.close(); return final_price_text
    except Exception as e:
        if page and not page.is_closed(): await page.close()
        return "Hata oluştu."

# --- Discord Bot Ana Kodları ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    global playwright, browser
    print(f'{client.user} olarak Discord\'a giriş yapıldı.')
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        print("✅ Tarayıcı (PlayStation için) başarıyla başlatıldı!")
    except Exception as e:
        print(f"❌ HATA: Playwright tarayıcısı başlatılamadı: {e}")

@client.event
async def on_message(message):
    if message.author == client.user: return
    if message.content.lower().startswith('!fiyat '):
        oyun_adi_orjinal = message.content[7:].strip() # Kullanıcının girdiği orijinal ismi sakla
        if not oyun_adi_orjinal: await message.channel.send("Lütfen bir oyun adı girin."); return
        
        # Arama yapmadan önce oyun adını temizle
        oyun_adi_temiz = clean_game_name(oyun_adi_orjinal)
        
        msg = await message.channel.send(f"**{oyun_adi_orjinal}** için mağazalar kontrol ediliyor...")
        
        # Tüm fonksiyonlara temizlenmiş ismi gönder
        steam_fiyati_task = asyncio.to_thread(get_steam_price, oyun_adi_temiz)
        epic_linki_task = asyncio.to_thread(get_epic_games_link, oyun_adi_temiz)
        ps_fiyati_task = get_playstation_price(oyun_adi_temiz)
        
        steam_sonucu, epic_linki, ps_fiyati = await asyncio.gather(
            steam_fiyati_task,
            epic_linki_task,
            ps_fiyati_task
        )
        
        # Embed başlığında kullanıcının girdiği orijinal ismi kullan
        embed = discord.Embed(title=f"🎮 {oyun_adi_orjinal} Fiyat Bilgisi ve Linkler", color=discord.Color.from_rgb(0, 112, 255))
        
        if isinstance(steam_sonucu, tuple):
            price, currency = steam_sonucu
            try_rate = get_usd_to_try_rate()
            if try_rate and currency == "USD":
                tl_price = price * try_rate
                final_price_text = f"${price:,.2f} {currency}\n(≈ {tl_price:,.2f} TL)"
            else: final_price_text = f"{price:,.2f} {currency}"
            embed.add_field(name="Steam", value=final_price_text, inline=True)
        elif steam_sonucu:
            embed.add_field(name="Steam", value=steam_sonucu, inline=True)
        
        if ps_fiyati:
            embed.add_field(name="PlayStation Store", value=ps_fiyati, inline=True)

        if epic_linki:
            embed.add_field(name="Epic Games", value=f"[Mağazada Ara]({epic_linki})", inline=True)
        
        await msg.edit(content=None, embed=embed)

# --- Botu ve Sunucuyu Başlatma ---
# Render'da keep_alive() çağırmana gerek yok ama kodda kalmasının bir zararı yok.
keep_alive() 
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
if DISCORD_TOKEN:
    client.run(DISCORD_TOKEN)
else:
    print("HATA: DISCORD_TOKEN bulunamadı.")