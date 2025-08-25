import discord
import os
from dotenv import load_dotenv
load_dotenv()
import requests
import json
from flask import Flask
from threading import Thread
from playwright.async_api import async_playwright
import asyncio
import time
import re
from datetime import datetime # DÜZELTİLDİ

# --- Global Değişkenler ---
playwright = None
browser = None
currency_cache = { "rate": None, "last_fetched": 0 }

# --- Loglama Yardımcı Fonksiyonu ---
def log_debug(func_name, message):
    """Log mesajlarını zaman damgası ve fonksiyon adıyla birlikte konsola yazdırır."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [DEBUG] [{func_name}] {message}")

# --- Web Sunucusu ve Keep Alive ---
app = Flask('')
@app.route('/')
def home(): return "Bot Aktif ve Çalışıyor!"
def run(): app.run(host='0.0.0.0', port=8080)
def keep_alive():
    t = Thread(target=run)
    t.start()

# --- Oyun Adı Temizleme Fonksiyonu ---
def clean_game_name(game_name):
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

# --- Steam Fiyat ve Link Alma Fonksiyonu ---
def get_steam_price(game_name):
    func_name = "Steam"
    start_time = time.time()
    log_debug(func_name, f"İstek başladı: '{game_name}'")
    try:
        search_url = f"https://store.steampowered.com/api/storesearch/?term={requests.utils.quote(game_name)}&l=turkish&cc=TR"
        log_debug(func_name, f"URL: {search_url}")
        response = requests.get(search_url, timeout=10)
        
        log_debug(func_name, f"HTTP Durum Kodu: {response.status_code}")
        if response.status_code != 200 or not response.json().get('items'):
            log_debug(func_name, "API'den geçerli sonuç alınamadı.")
            return None

        search_results = response.json().get('items', [])
        if not search_results: 
            log_debug(func_name, "Arama sonucu bulunamadı.")
            return None

        best_match = search_results[0]
        link = f"https://store.steampowered.com/app/{best_match.get('id')}"
        game_name_from_steam = best_match.get('name')

        price_data = best_match.get('price')
        if not price_data:
            price_info_text = "Ücretsiz!" if not best_match.get('unpurchaseable') else "Fiyat bilgisi yok."
            log_debug(func_name, f"Sonuç bulundu: {price_info_text}")
            return {"price": price_info_text, "link": link, "name": game_name_from_steam}

        price_float = None
        if isinstance(price_data, dict):
            final_price = price_data.get('final')
            if isinstance(final_price, int):
                price_float = final_price / 100.0

        if price_float is not None:
            log_debug(func_name, f"Fiyat bulundu: ${price_float} USD")
            return {"price": (price_float, "USD"), "link": link, "name": game_name_from_steam}
        else:
            log_debug(func_name, "Fiyat bilgisi ayrıştırılamadı.")
            return {"price": "Fiyat bilgisi yok.", "link": link, "name": game_name_from_steam}

    except Exception as e:
        log_debug(func_name, f"HATA OLUŞTU: {e}")
        return None
    finally:
        duration = time.time() - start_time
        log_debug(func_name, f"İstek {duration:.2f} saniyede tamamlandı.")

# --- Epic Games Link Bulma Fonksiyonu ---
def get_epic_games_link(game_name):
    query = requests.utils.quote(game_name)
    return f"https://store.epicgames.com/tr/browse?q={query}&sortBy=relevancy&sortDir=DESC"

# --- PlayStation Store Fiyat ve Link Alma Fonksiyonu ---
async def get_playstation_price(game_name):
    func_name = "PlayStation"
    start_time = time.time()
    log_debug(func_name, f"İstek başladı: '{game_name}'")
    global browser
    if not browser or not browser.is_connected(): 
        log_debug(func_name, "Tarayıcı bulunamadı veya bağlı değil.")
        return None
    page = None
    try:
        page = await browser.new_page()
        page.set_default_timeout(90000)
        search_url = f"https://store.playstation.com/tr-tr/search/{requests.utils.quote(game_name)}"
        log_debug(func_name, f"Arama sayfasına gidiliyor: {search_url}")
        await page.goto(search_url)

        results_selector = 'div[data-qa^="search#productTile"]'
        await page.wait_for_selector(results_selector, timeout=15000)
        log_debug(func_name, "Arama sonuçları bulundu.")

        all_results = await page.locator(results_selector).all()
        if not all_results:
            await page.close()
            return None

        exact_match = None
        startswith_match = None

        for result in all_results:
            try:
                title_selector = 'span[data-qa$="product-name"]'
                title_element = result.locator(title_selector)
                if await title_element.count() > 0:
                    title_text = await title_element.inner_text()
                    cleaned_title = clean_game_name(title_text)
                    if cleaned_title == game_name:
                        exact_match = result
                        break
                    if cleaned_title.startswith(game_name) and not startswith_match:
                        startswith_match = result
            except Exception:
                continue

        best_match_element = exact_match or startswith_match or all_results[0]
        await best_match_element.locator('a.psw-link').first().click()
        log_debug(func_name, "En iyi sonuca tıklandı, ürün sayfasına gidiliyor.")
        
        await page.wait_for_load_state('networkidle')
        log_debug(func_name, "Ürün sayfası yüklendi. Fiyat aranıyor.")
        
        link = page.url
        price_info = "Fiyat bilgisi yok."
        is_in_plus = False
        is_in_ea_play = False

        offer0_price_selector = 'span[data-qa="mfeCtaMain#offer0#finalPrice"]'
        offer0_element = page.locator(offer0_price_selector).first
        if await offer0_element.count() > 0:
            offer0_text = await offer0_element.inner_text()
            if "Dahil" in offer0_text or "Oyna" in offer0_text:
                is_in_plus = True
                original_price_selector = 'span[data-qa="mfeCtaMain#offer0#originalPrice"]'
                original_price_element = page.locator(original_price_selector).first
                if await original_price_element.count() > 0:
                    price_info = await original_price_element.inner_text()
                else:
                    price_info = offer0_text
        
        ea_play_selector = 'span[data-qa="mfeCtaMain#offer2#discountInfo"]'
        ea_play_element = page.locator(ea_play_selector).first
        if await ea_play_element.count() > 0:
            ea_play_text = await ea_play_element.inner_text()
            if "EA Play" in ea_play_text:
                is_in_ea_play = True

        purchase_price_selector = 'span[data-qa="mfeCtaMain#offer1#finalPrice"]'
        purchase_price_element = page.locator(purchase_price_selector).first
        if await purchase_price_element.count() > 0:
            price_info = await purchase_price_element.inner_text()
        elif not is_in_plus and await offer0_element.count() > 0:
            price_info = await offer0_element.inner_text()

        final_price_text = price_info
        if is_in_plus:
            if "Dahil" not in final_price_text and "Oyna" not in final_price_text:
                 final_price_text += "\n*PS Plus'a Dahil*"
        if is_in_ea_play:
            final_price_text += "\n*EA Play'e Dahil*"
            
        log_debug(func_name, f"Sonuç bulundu: '{final_price_text.replace('\n', ' ')}'")
        await page.close()
        return {"price": final_price_text, "link": link}

    except Exception as e:
        log_debug(func_name, f"HATA OLUŞTU: {e}")
        if page and not page.is_closed(): await page.close()
        return None
    finally:
        duration = time.time() - start_time
        log_debug(func_name, f"İstek {duration:.2f} saniyede tamamlandı.")
        
# --- Xbox Store Fiyat ve Link Alma Fonksiyonu ---
async def get_xbox_price(game_name_clean):
    func_name = "Xbox"
    start_time = time.time()
    log_debug(func_name, f"İstek başladı: '{game_name_clean}'") # DÜZELTİLDİ
    global browser
    if not browser or not browser.is_connected(): 
        log_debug(func_name, "Tarayıcı bulunamadı veya bağlı değil.")
        return None
    page = None
    try:
        page = await browser.new_page()
        page.set_default_timeout(90000)
        search_url = f"https://www.xbox.com/tr-TR/Search/Results?q={requests.utils.quote(game_name_clean)}"
        log_debug(func_name, f"Arama sayfasına gidiliyor: {search_url}")
        await page.goto(search_url)
        
        await page.wait_for_selector('div[class*="ProductCard-module"]')
        log_debug(func_name, "Arama sonuçları bulundu.")
        results = await page.query_selector_all('a[class*="commonStyles-module__basicButton"]')
        if not results: 
            await page.close()
            return None

        target_link = None
        for result in results:
            aria_label = await result.get_attribute("aria-label") or ""
            if clean_game_name(aria_label).startswith(game_name_clean):
                target_link = result
                break

        if not target_link: 
            log_debug(func_name, "Eşleşen ürün linki bulunamadı.")
            await page.close()
            return None
        
        await target_link.click()
        log_debug(func_name, "En iyi sonuca tıklandı, ürün sayfasına gidiliyor.")
        await page.wait_for_load_state('networkidle')
        log_debug(func_name, "Ürün sayfası yüklendi. Fiyat aranıyor.")
        link = page.url

        price_info = "Fiyat bilgisi yok."
        is_on_game_pass = False
        
        game_pass_selector = 'svg[aria-label="Game Pass ile birlikte gelir"]'
        if await page.locator(game_pass_selector).count() > 0:
            is_on_game_pass = True
        
        price_selector = 'span[class*="Price-module__boldText"]'
        price_element = page.locator(price_selector).first
        if await price_element.count() > 0:
            price_text = await price_element.inner_text()
            price_info = price_text

        final_result = price_info
        if is_on_game_pass:
            if price_info != "Fiyat bilgisi yok.":
                final_result = f"{price_info}\n*Game Pass'e Dahil*"
            else:
                final_result = "Game Pass'e Dahil"
        
        log_debug(func_name, f"Sonuç bulundu: '{final_result.replace('\n', ' ')}'")
        await page.close()
        return {"price": final_result, "link": link}
    except Exception as e:
        log_debug(func_name, f"HATA OLUŞTU: {e}")
        if page and not page.is_closed(): await page.close()
        return None
    finally:
        duration = time.time() - start_time
        log_debug(func_name, f"İstek {duration:.2f} saniyede tamamlandı.")

# --- Allkeyshop Fiyat ve Link Alma Fonksiyonu ---
def get_allkeyshop_price(game_name):
    func_name = "Allkeyshop"
    start_time = time.time()
    log_debug(func_name, f"İstek başladı: '{game_name}'")
    try:
        formatted_game_name = game_name.replace(' ', '-')
        url = f"https://www.allkeyshop.com/blog/en-us/buy-{formatted_game_name}-cd-key-compare-prices/"
        log_debug(func_name, f"URL: {url}")
        
        headers = { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36' }
        response = requests.get(url, headers=headers, timeout=15)
        
        log_debug(func_name, f"HTTP Durum Kodu: {response.status_code}")
        if response.status_code != 200:
            log_debug(func_name, "Başarısız durum kodu nedeniyle işlem durduruldu.")
            return None

        pattern = re.compile(r'<p class="faq-answer" data-itemprop="acceptedAnswer">.*?\$(\d+\.\d{2}).*?</p>', re.DOTALL)
        match = pattern.search(response.text)

        if match:
            price_str = match.group(1)
            price_float = float(price_str)
            log_debug(func_name, f"Fiyat bulundu: ${price_float}")
            return {"price": (price_float, "USD"), "link": url}
        
        log_debug(func_name, "Sayfa içeriğinde fiyat deseni bulunamadı.")
        return None
        
    except Exception as e:
        log_debug(func_name, f"HATA OLUŞTU: {e}")
        return None
    finally:
        duration = time.time() - start_time
        log_debug(func_name, f"İstek {duration:.2f} saniyede tamamlandı.")

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
        print("✅ Tarayıcı (PS & Xbox için) başarıyla başlatıldı!")
    except Exception as e:
        print(f"❌ HATA: Playwright tarayıcısı başlatılamadı: {e}")

@client.event
async def on_message(message):
    if message.author == client.user: return
    if message.content.lower().startswith('!fiyat '):
        oyun_adi_orjinal = message.content[7:].strip()
        if not oyun_adi_orjinal: await message.channel.send("Lütfen bir oyun adı girin."); return
        
        log_debug("on_message", f"Yeni istek: '{oyun_adi_orjinal}' | Kullanıcı: {message.author}")
        oyun_adi_temiz = clean_game_name(oyun_adi_orjinal)
        msg = await message.channel.send(f"**{oyun_adi_orjinal}** için mağazalar kontrol ediliyor...")

        tasks = {
            "steam": asyncio.to_thread(get_steam_price, oyun_adi_temiz),
            "epic": asyncio.to_thread(get_epic_games_link, oyun_adi_temiz),
            "ps": get_playstation_price(oyun_adi_temiz),
            "xbox": get_xbox_price(oyun_adi_temiz),
            "allkeyshop": asyncio.to_thread(get_allkeyshop_price, oyun_adi_temiz)
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        sonuclar = dict(zip(tasks.keys(), results))
        log_debug("on_message", "Tüm mağaza görevleri tamamlandı. Sonuçlar formatlanıyor.")

        display_game_name = oyun_adi_orjinal
        steam_sonucu = sonuclar.get("steam")
        if isinstance(steam_sonucu, dict) and steam_sonucu.get("name"):
            display_game_name = steam_sonucu['name']

        embed = discord.Embed(title=f"🎮 {display_game_name} Fiyat Bilgisi ve Linkler - V.0.32", color=discord.Color.from_rgb(16, 124, 16))

        # --- Steam Sonucu ---
        if isinstance(steam_sonucu, dict):
            price_info, link = steam_sonucu["price"], steam_sonucu["link"]
            if isinstance(price_info, tuple):
                price, currency = price_info
                try_rate = get_usd_to_try_rate()
                if try_rate and currency == "USD":
                    tl_price = price * try_rate
                    display_text = f"${price:,.2f} {currency}\n(≈ {tl_price:,.2f} TL)"
                else: display_text = f"{price:,.2f} {currency}"
                embed.add_field(name="Steam", value=f"[{display_text}]({link})", inline=True)
            else: embed.add_field(name="Steam", value=f"[{price_info}]({link})", inline=True)

        # --- Allkeyshop Sonucu ---
        allkeyshop_sonucu = sonuclar.get("allkeyshop")
        if isinstance(allkeyshop_sonucu, dict):
            price_info, link = allkeyshop_sonucu["price"], allkeyshop_sonucu["link"]
            if isinstance(price_info, tuple):
                price, currency = price_info
                try_rate = get_usd_to_try_rate()
                if try_rate and currency == "USD":
                    tl_price = price * try_rate
                    display_text = f"${price:,.2f} {currency}\n(≈ {tl_price:,.2f} TL)"
                else:
                    display_text = f"${price:,.2f} {currency}"
                embed.add_field(name="Allkeyshop (CD-Key)", value=f"[{display_text}]({link})", inline=True)
            else:
                embed.add_field(name="Allkeyshop (CD-Key)", value=f"[{price_info}]({link})", inline=True)

        # --- PlayStation Sonucu ---
        ps_sonucu = sonuclar.get("ps")
        if isinstance(ps_sonucu, dict):
            embed.add_field(name="PlayStation Store", value=f'[{ps_sonucu["price"]}]({ps_sonucu["link"]})', inline=True)

        # --- Xbox Sonucu ---
        xbox_sonucu = sonuclar.get("xbox")
        if isinstance(xbox_sonucu, dict):
            embed.add_field(name="Xbox Store", value=f'[{xbox_sonucu["price"]}]({xbox_sonucu["link"]})', inline=True)

        # --- Epic Games Sonucu ---
        epic_linki = sonuclar.get("epic")
        if epic_linki:
            embed.add_field(name="Epic Games", value=f"[Mağazada Ara]({epic_linki})", inline=True)

        await msg.edit(content=None, embed=embed)
        log_debug("on_message", f"Sonuçlar kullanıcıya gönderildi: '{oyun_adi_orjinal}'")

# --- Botu ve Sunucuyu Başlatma ---
# keep_alive() 
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
if DISCORD_TOKEN:
    client.run(DISCORD_TOKEN)
else:
    print("HATA: DISCORD_TOKEN bulunamadı.")