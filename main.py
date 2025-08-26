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
import logging
from datetime import datetime
from playwright_stealth import stealth_async

# --- Debug ve Hata Ayıklama Kurulumu ---
if not os.path.exists('debug_output'):
    os.makedirs('debug_output')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
    handlers=[
        logging.FileHandler("debug_output/bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# --- Global Değişkenler ---
playwright = None
browser = None
currency_cache = { "rate": None, "last_fetched": 0 }

# --- Web Sunucusu ve Keep Alive ---
app = Flask('')
@app.route('/')
def home(): return "Bot Aktif ve Çalışıyor!"
def run(): app.run(host='0.0.0.0', port=8080)
def keep_alive():
    t = Thread(target=run)
    t.start()
    
# --- Stealth (Gizli) Sayfa Oluşturma Yardımcısı ---
async def create_stealth_page():
    global browser
    page = await browser.new_page()
    await stealth_async(page)
    return page

# --- Romen Rakamı ve Sayı Çıkarma Yardımcıları ---
def clean_and_extract_roman(name):
    name = name.upper()
    if name.endswith(" IV"): return name.replace(" IV", " 4"), 4
    if name.endswith(" IX"): return name.replace(" IX", " 9"), 9
    if name.endswith(" V"): return name.replace(" V", " 5"), 5
    if name.endswith(" III"): return name.replace(" III", " 3"), 3
    if name.endswith(" II"): return name.replace(" II", " 2"), 2
    if name.endswith(" I"): return name.replace(" I", " 1"), 1
    return name.lower(), None

def extract_numbers_from_title(title):
    numbers = set(map(int, re.findall(r'\d+', title)))
    title_upper = f" {title.upper()} "
    if " II " in title_upper or title_upper.endswith(" II"): numbers.add(2)
    if " III " in title_upper or title_upper.endswith(" III"): numbers.add(3)
    if " IV " in title_upper or title_upper.endswith(" IV"): numbers.add(4)
    if " V " in title_upper or title_upper.endswith(" V"): numbers.add(5)
    return numbers

# --- Oyun Adı Temizleme Fonksiyonu ---
def clean_game_name(game_name):
    name_with_arabic, _ = clean_and_extract_roman(game_name)
    cleaned_name = re.sub(r'[^\w\s]', ' ', name_with_arabic, flags=re.UNICODE)
    cleaned_name = re.sub(r'\s+', ' ', cleaned_name)
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
                    logging.info(f"Yeni USD/TRY kuru alındı: {rate}")
                    return rate
            logging.warning(f"Döviz kuru alınamadı. Status Code: {response.status_code}")
            return currency_cache["rate"]
        except Exception as e:
            logging.error(f"Döviz kuru alınırken hata: {e}")
            return currency_cache["rate"]
    return currency_cache["rate"]

# --- Steam Fiyat ve Link Alma Fonksiyonu ---
def get_steam_price(game_name):
    try:
        user_query_numbers = extract_numbers_from_title(game_name)
        search_url = f"https://store.steampowered.com/api/storesearch/?term={requests.utils.quote(game_name)}&l=turkish&cc=TR"
        response = requests.get(search_url)
        if response.status_code != 200 or not response.json().get('items'): return None
        search_results = response.json().get('items', [])
        if not search_results: return None
        best_match = None
        highest_score = -1
        for item in search_results:
            item_name = item.get('name', '')
            cleaned_item_name = clean_game_name(item_name)
            current_score = 0
            if game_name in cleaned_item_name: current_score += 90
            elif cleaned_item_name in game_name: current_score += 85
            else: continue
            result_numbers = extract_numbers_from_title(cleaned_item_name)
            if user_query_numbers:
                if not user_query_numbers.intersection(result_numbers): current_score -= 100
            else:
                if any(n > 1 for n in result_numbers): current_score -= 100
            if current_score > highest_score:
                highest_score = current_score
                best_match = item
        if not best_match or highest_score < 50: return None
        link = f"https://store.steampowered.com/app/{best_match.get('id')}"
        game_name_from_steam = best_match.get('name')
        price_data = best_match.get('price')
        if not price_data:
            if best_match.get('unpurchaseable'): return {"price": "Fiyat bilgisi yok.", "link": link, "name": game_name_from_steam}
            else: return {"price": "Ücretsiz!", "link": link, "name": game_name_from_steam}
        price_float = None
        if isinstance(price_data, dict):
            final_price = price_data.get('final')
            if isinstance(final_price, int): price_float = final_price / 100.0
        if price_float is not None:
            return {"price": (price_float, "USD"), "link": link, "name": game_name_from_steam}
        else:
            return {"price": "Fiyat bilgisi yok.", "link": link, "name": game_name_from_steam}
    except Exception as e:
        logging.error(f"STEAM HATA: {e}", exc_info=True)
        return None

# --- Epic Games Link Bulma Fonksiyonu ---
def get_epic_games_link(game_name):
    query = requests.utils.quote(game_name)
    return f"https://store.epicgames.com/tr/browse?q={query}&sortBy=relevancy&sortDir=DESC"

# --- Hata durumunda ekran görüntüsü alan yardımcı fonksiyon ---
async def take_screenshot_on_error(page, platform_name, game_name):
    if page and not page.is_closed():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = f"debug_output/error_{platform_name}_{game_name.replace(' ', '_')}_{timestamp}.png"
        await page.screenshot(path=screenshot_path)
        logging.info(f"Hata ekran görüntüsü kaydedildi: {screenshot_path}")

# --- PlayStation Store Fiyat ve Link Alma Fonksiyonu ---
async def get_playstation_price(game_name):
    if not browser or not browser.is_connected(): return None
    page = None
    try:
        page = await create_stealth_page()
        await page.goto(f"https://store.playstation.com/tr-tr/search/{requests.utils.quote(game_name)}", wait_until='domcontentloaded')
        try:
            cookie_button = page.locator('button:has-text("Accept All Cookies"), button:has-text("Tümünü Kabul Et")')
            if await cookie_button.count() > 0:
                await cookie_button.first.click(timeout=5000)
                await page.wait_for_timeout(2000)
        except Exception:
            logging.info("Cookie banner'ı bulunamadı/tıklanamadı.")
        await page.wait_for_selector('div[data-qa^="search#productTile"]', timeout=20000)
        all_results = await page.locator('div[data-qa^="search#productTile"]').all()
        if not all_results: await page.close(); return None
        user_query_numbers = extract_numbers_from_title(game_name)
        best_match_element = None; highest_score = -1
        for result in all_results:
            try:
                title_element = result.locator('span[data-qa$="product-name"]')
                if await title_element.count() == 0: continue
                item_name = await title_element.inner_text()
                cleaned_item_name = clean_game_name(item_name)
                base_score = 100; current_score = 0
                if cleaned_item_name.startswith(game_name): current_score = base_score - 5
                elif game_name in cleaned_item_name: current_score = base_score - 10
                else: continue
                current_score -= (len(cleaned_item_name) - len(game_name))
                result_numbers = extract_numbers_from_title(cleaned_item_name)
                if user_query_numbers:
                    if not user_query_numbers.intersection(result_numbers): current_score = -1
                else:
                    if any(n > 1 for n in result_numbers): current_score = -1
                if current_score > highest_score:
                    highest_score = current_score; best_match_element = result
            except Exception: continue
        if not best_match_element or highest_score < 50:
            await page.close(); return None
        price_info = "Fiyat bilgisi yok."; subscriptions = []
        card_text = await best_match_element.inner_text()
        price_match = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2}\s*TL)', card_text)
        if price_match: price_info = price_match.group(1)
        if "Extra" in card_text or "Premium" in card_text: subscriptions.append("PS Plus'a Dahil")
        if "GTA+" in card_text: subscriptions.append("GTA+'a Dahil")
        if "EA Play" in card_text: subscriptions.append("EA Play'e Dahil")
        link_element = best_match_element.locator('a.psw-link').first
        href = await link_element.get_attribute('href')
        link = "https://store.playstation.com" + href
        final_display_text = price_info
        if subscriptions:
            if final_display_text == "Fiyat bilgisi yok.": final_display_text = "Dahil"
            subscription_text = "\n*" + " & ".join(sorted(subscriptions)) + "*"
            if "Dahil" in final_display_text: final_display_text = "*" + " & ".join(sorted(subscriptions)) + "*"
            else: final_display_text = (final_display_text + subscription_text).strip()
        await page.close()
        return {"price": final_display_text, "link": link}
    except Exception as e:
        logging.error(f"PLAYSTATION HATA: {e}", exc_info=True)
        if page: await take_screenshot_on_error(page, "playstation", game_name)
        if page and not page.is_closed(): await page.close()
        return None

# --- Xbox Store Fiyat ve Link Alma Fonksiyonu ---
async def get_xbox_price(game_name_clean):
    if not browser or not browser.is_connected(): return None
    page = None
    try:
        page = await create_stealth_page()
        await page.goto(f"https://www.xbox.com/tr-TR/Search/Results?q={requests.utils.quote(game_name_clean)}")
        await page.wait_for_selector('div[class*="ProductCard-module"]')
        all_results = await page.locator('a[class*="commonStyles-module__basicButton"]').all()
        if not all_results: await page.close(); return None
        user_query_numbers = extract_numbers_from_title(game_name_clean)
        best_match_element = None; highest_score = -1
        for result in all_results:
            full_aria_label = await result.get_attribute("aria-label") or ""
            if not full_aria_label: continue
            item_name = full_aria_label.split(',')[0].strip()
            cleaned_item_name = clean_game_name(item_name)
            current_score = 0
            if game_name_clean in cleaned_item_name: current_score += 90
            elif cleaned_item_name in game_name_clean: current_score += 85
            else: continue
            result_numbers = extract_numbers_from_title(cleaned_item_name)
            if user_query_numbers:
                if not user_query_numbers.intersection(result_numbers): current_score -= 100
            else:
                if any(n > 1 for n in result_numbers): current_score -= 100
            if current_score > highest_score:
                highest_score = current_score; best_match_element = result
        if not best_match_element or highest_score < 50:
            await page.close(); return None
        await best_match_element.click()
        await page.wait_for_load_state('networkidle')
        link = page.url
        price_info = "Fiyat bilgisi yok."; subscriptions = []
        if await page.locator('svg[aria-label="Game Pass ile birlikte gelir"]').count() > 0:
            subscriptions.append("Game Pass'e Dahil")
        if await page.locator('*:has-text("GTA+ ile birlikte gelir")').count() > 0:
            if "GTA+ ile birlikte gelir" not in subscriptions:
                 subscriptions.append("GTA+ ile birlikte gelir")
        price_element = page.locator('span[class*="Price-module__boldText"]').first
        if await price_element.count() > 0:
            price_info = await price_element.inner_text()
        await page.close()
        final_display_text = price_info
        if subscriptions:
            if final_display_text == "Fiyat bilgisi yok.": final_display_text = ""
            subscription_text = "\n*" + " & ".join(subscriptions) + "*"
            final_display_text = (final_display_text + subscription_text).strip()
        return {"price": final_display_text, "link": link}
    except Exception as e:
        logging.error(f"XBOX HATA: {e}", exc_info=True)
        if page: await take_screenshot_on_error(page, "xbox", game_name_clean)
        if page and not page.is_closed(): await page.close()
        return None

# --- Allkeyshop Fiyat ve Link Alma Fonksiyonu ---
async def get_allkeyshop_price(game_name):
    if not browser or not browser.is_connected(): return None
    page = None
    try:
        page = await create_stealth_page()
        formatted_game_name = game_name.replace(' ', '-')
        urls_to_try = [
            f"https://www.allkeyshop.com/blog/en-us/buy-{formatted_game_name}-cd-key-compare-prices/",
            f"https://www.allkeyshop.com/blog/en-us/compare-and-buy-cd-key-for-digital-download-{formatted_game_name}/"
        ]
        for i, url in enumerate(urls_to_try):
            try:
                await page.goto(url, timeout=45000, wait_until='domcontentloaded')
                html_content = await page.content()
                pattern = re.search(r"var gamePageTrans = ({.*?});", html_content, re.DOTALL)
                if not pattern: continue
                data = json.loads(pattern.group(1))
                prices_list = data.get("prices")
                if not prices_list: continue
                key_offers = [o for o in prices_list if not o.get('account') and 'priceCard' in o]
                if not key_offers: return None
                lowest_price = min(float(o['priceCard']) for o in key_offers)
                return {"price": (lowest_price, "USD"), "link": url}
            except Exception:
                continue
        return None
    except Exception as e:
        logging.error(f"ALLKEYSHOP HATA: {e}", exc_info=False)
        if page: await take_screenshot_on_error(page, "allkeyshop", game_name)
        return None
    finally:
        if page and not page.is_closed(): await page.close()

# --- Discord Bot Ana Kodları ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    global playwright, browser
    logging.info(f'{client.user} olarak Discord\'a giriş yapıldı.')
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        logging.info("✅ Tarayıcı (PS & Xbox için) başarıyla başlatıldı!")
    except Exception as e:
        logging.error(f"❌ HATA: Playwright tarayıcısı başlatılamadı: {e}", exc_info=True)

@client.event
async def on_message(message):
    if message.author == client.user: return
    if message.content.lower().startswith('!fiyat '):
        oyun_adi_orjinal = message.content[7:].strip()
        if not oyun_adi_orjinal: await message.channel.send("Lütfen bir oyun adı girin."); return
        oyun_adi_temiz = clean_game_name(oyun_adi_orjinal)
        msg = await message.channel.send(f"**{oyun_adi_orjinal}** için mağazalar kontrol ediliyor...")
        logging.info(f"Fiyat sorgusu başlatıldı: '{oyun_adi_orjinal}' (Temizlenmiş: '{oyun_adi_temiz}')")
        tasks = {
            "steam": asyncio.to_thread(get_steam_price, oyun_adi_temiz),
            "epic": asyncio.to_thread(get_epic_games_link, oyun_adi_temiz),
            "ps": get_playstation_price(oyun_adi_temiz),
            "xbox": get_xbox_price(oyun_adi_temiz),
            "allkeyshop": get_allkeyshop_price(oyun_adi_temiz)
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        sonuclar = dict(zip(tasks.keys(), results))
        display_game_name = oyun_adi_orjinal
        steam_sonucu = sonuclar.get("steam")
        if isinstance(steam_sonucu, dict) and steam_sonucu.get("name"):
            display_game_name = steam_sonucu['name']
        embed = discord.Embed(title=f"🎮 {display_game_name} Fiyat Bilgisi ve Linkler", color=discord.Color.from_rgb(16, 124, 16))
        embed.set_footer(text="Fiyatlar anlık olarak mağazalardan çekilmektedir.")
        store_order = ["steam", "allkeyshop", "ps", "xbox", "epic"]
        for store in store_order:
            result = sonuclar.get(store)
            store_name = {"steam": "Steam", "allkeyshop": "Allkeyshop (CD-Key)", "ps": "PlayStation Store", "xbox": "Xbox Store", "epic": "Epic Games"}[store]
            if isinstance(result, Exception):
                embed.add_field(name=store_name, value="`Hata oluştu.`", inline=True)
                logging.error(f"'{store}' deposu için sonuç işlenirken hata yakalandı: {result}", exc_info=result)
            elif result is None:
                 embed.add_field(name=store_name, value="`Bulunamadı.`", inline=True)
            elif store == "epic":
                embed.add_field(name=store_name, value=f"[Mağazada Ara]({result})", inline=True)
            else:
                price_info, link = result["price"], result["link"]
                display_text = ""
                if isinstance(price_info, tuple):
                    price, currency = price_info
                    try_rate = get_usd_to_try_rate()
                    if try_rate and currency == "USD":
                        tl_price = price * try_rate
                        display_text = f"${price:,.2f} {currency}\n(≈ {tl_price:,.2f} TL)"
                    else: 
                        display_text = f"${price:,.2f} {currency}"
                else:
                    display_text = str(price_info)
                embed.add_field(name=store_name, value=f"[{display_text}]({link})", inline=True)
        await msg.edit(content=None, embed=embed)

# --- Botu ve Sunucuyu Başlatma ---
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
if DISCORD_TOKEN:
    client.run(DISCORD_TOKEN)
else:
    logging.critical("HATA: DISCORD_TOKEN .env dosyasında bulunamadı.")