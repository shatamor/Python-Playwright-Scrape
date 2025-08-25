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

# --- YENİ: Debug ve Hata Ayıklama Kurulumu ---
# Replit'te çalışırken logları ve ekran görüntülerini saklamak için bir klasör oluşturalım.
if not os.path.exists('debug_output'):
    os.makedirs('debug_output')

# Loglama yapılandırması: Hem dosyaya hem de konsola log basacak.
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
                    logging.info(f"Yeni USD/TRY kuru alındı: {rate}")
                    return rate
            logging.warning(f"Döviz kuru alınamadı. Status Code: {response.status_code}")
            return currency_cache["rate"]
        except Exception as e:
            logging.error(f"Döviz kuru alınırken hata: {e}")
            return currency_cache["rate"]
    else: return currency_cache["rate"]

# --- Steam Fiyat ve Link Alma Fonksiyonu ---
def get_steam_price(game_name):
    try:
        search_url = f"https://store.steampowered.com/api/storesearch/?term={requests.utils.quote(game_name)}&l=turkish&cc=TR"
        response = requests.get(search_url)
        if response.status_code != 200 or not response.json().get('items'):
            logging.warning(f"Steam araması başarısız oldu. Status Code: {response.status_code}, Game: {game_name}")
            return None

        search_results = response.json().get('items', [])
        if not search_results:
            logging.info(f"Steam'de '{game_name}' için sonuç bulunamadı.")
            return None

        best_match = search_results[0]
        link = f"https://store.steampowered.com/app/{best_match.get('id')}"
        game_name_from_steam = best_match.get('name')
        price_data = best_match.get('price')

        if not price_data:
            if best_match.get('unpurchaseable'):
                 return {"price": "Fiyat bilgisi yok.", "link": link, "name": game_name_from_steam}
            else:
                 return {"price": "Ücretsiz!", "link": link, "name": game_name_from_steam}

        price_float = None
        if isinstance(price_data, dict):
            final_price = price_data.get('final')
            if isinstance(final_price, int):
                price_float = final_price / 100.0

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


# --- YENİ: Hata durumunda ekran görüntüsü alan yardımcı fonksiyon ---
async def take_screenshot_on_error(page, platform_name, game_name):
    if page and not page.is_closed():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = f"debug_output/error_{platform_name}_{game_name.replace(' ', '_')}_{timestamp}.png"
        await page.screenshot(path=screenshot_path)
        logging.info(f"Hata ekran görüntüsü kaydedildi: {screenshot_path}")


# --- PlayStation Store Fiyat ve Link Alma Fonksiyonu (DEBUG EKLENDİ) ---
async def get_playstation_price(game_name):
    global browser
    if not browser or not browser.is_connected():
        logging.warning("PlayStation fiyatı alınamıyor: Tarayıcı bağlı değil.")
        return None
    page = None
    try:
        page = await browser.new_page()
        page.set_default_timeout(90000)
        search_url = f"https://store.playstation.com/tr-tr/search/{requests.utils.quote(game_name)}"
        logging.info(f"PlayStation için gidiliyor: {search_url}")
        await page.goto(search_url)

        results_selector = 'div[data-qa^="search#productTile"]'
        await page.wait_for_selector(results_selector, timeout=15000)

        all_results = await page.locator(results_selector).all()
        if not all_results:
            await page.close()
            logging.warning(f"PlayStation'da '{game_name}' için arama sonucu bulunamadı.")
            return None

        # ... (Eşleşme mantığınız aynı kalıyor) ...
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
        await best_match_element.locator('a.psw-link').first.click()
        await page.wait_for_selector('span[data-qa^="mfeCtaMain#offer"]')
        link = page.url

        price_info = "Fiyat bilgisi yok."
        is_in_plus = False
        is_in_ea_play = False

        # ... (Fiyat alma mantığınız aynı kalıyor) ...
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

        await page.close()
        return {"price": final_price_text, "link": link}

    except Exception as e:
        logging.error(f"PLAYSTATION HATA: {e}", exc_info=True)
        # YENİ: Hata anında ekran görüntüsü al
        await take_screenshot_on_error(page, "playstation", game_name)
        if page and not page.is_closed(): await page.close()
        return None

# --- Xbox Store Fiyat ve Link Alma Fonksiyonu (DEBUG EKLENDİ) ---
async def get_xbox_price(game_name_clean):
    global browser
    if not browser or not browser.is_connected():
        logging.warning("Xbox fiyatı alınamıyor: Tarayıcı bağlı değil.")
        return None
    page = None
    try:
        page = await browser.new_page()
        page.set_default_timeout(90000)
        search_url = f"https://www.xbox.com/tr-TR/Search/Results?q={requests.utils.quote(game_name_clean)}"
        logging.info(f"Xbox için gidiliyor: {search_url}")
        await page.goto(search_url)
        await page.wait_for_selector('div[class*="ProductCard-module"]')

        # ... (Mantığınız aynı kalıyor) ...
        results = await page.query_selector_all('a[class*="commonStyles-module__basicButton"]')
        if not results: await page.close(); return None
        target_link = None
        for result in results:
            aria_label = await result.get_attribute("aria-label") or ""
            if clean_game_name(aria_label).startswith(game_name_clean):
                target_link = result
                break
        if not target_link: await page.close(); return None
        await target_link.click()
        await page.wait_for_load_state('networkidle')
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
        await page.close()
        if is_on_game_pass:
            if price_info != "Fiyat bilgisi yok.":
                return {"price": f"{price_info}\n*Game Pass'e Dahil*", "link": link}
            else:
                return {"price": "Game Pass'e Dahil", "link": link}
        return {"price": price_info, "link": link}
    except Exception as e:
        logging.error(f"XBOX HATA: {e}", exc_info=True)
        # YENİ: Hata anında ekran görüntüsü al
        await take_screenshot_on_error(page, "xbox", game_name_clean)
        if page and not page.is_closed(): await page.close()
        return None

# --- Allkeyshop Fiyat ve Link Alma Fonksiyonu (HESAP SATIŞI FİLTRESİ EKLENDİ) ---
def get_allkeyshop_price(game_name):
    try:
        formatted_game_name = game_name.replace(' ', '-')
        url = f"https://www.allkeyshop.com/blog/en-us/buy-{formatted_game_name}-cd-key-compare-prices/"
        logging.info(f"Allkeyshop için gidiliyor: {url}")

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)

        if response.status_code != 200:
            logging.warning(f"Allkeyshop'tan '{game_name}' alınamadı. Status Code: {response.status_code}")
            return None

        pattern = re.search(r"var gamePageTrans = ({.*?});", response.text, re.DOTALL)

        if pattern:
            json_data_str = pattern.group(1)
            try:
                data = json.loads(json_data_str)
                prices_list = data.get("prices")

                if not prices_list or not isinstance(prices_list, list):
                    logging.warning(f"Allkeyshop JSON verisinde 'prices' listesi bulunamadı. Oyun: '{game_name}'")
                    return None

                # YENİ: Sadece 'anahtar' (key) satışlarını almak için filtreleme yapıyoruz.
                # "account" değeri 'false' olan teklifleri seçiyoruz.
                key_offers = [
                    offer for offer in prices_list
                    if offer.get('account') is False and 'price' in offer
                ]

                if not key_offers:
                    logging.warning(f"Allkeyshop'ta '{game_name}' için anahtar (key) teklifi bulunamadı. Yalnızca hesap satışları olabilir.")
                    return None

                # Filtrelenmiş anahtar teklifleri arasından en ucuzunu bulalım.
                lowest_price = min(float(offer['price']) for offer in key_offers)

                logging.info(f"Allkeyshop için en düşük ANAHTAR fiyatı bulundu: {lowest_price} USD")
                return {"price": (lowest_price, "USD"), "link": url}

            except json.JSONDecodeError:
                logging.error(f"Allkeyshop için JSON verisi ayrıştırılamadı. Oyun: '{game_name}'")
                with open("debug_output/allkeyshop_json_error.html", "w", encoding='utf-8') as f:
                    f.write(response.text)
                return None
            except (ValueError, TypeError):
                 logging.error(f"Allkeyshop anahtar fiyat listesi beklenmedik bir formatta geldi. Oyun: '{game_name}'")
                 return None
        else:
            logging.warning(f"Allkeyshop için 'gamePageTrans' JavaScript bloğu bulunamadı. Oyun: '{game_name}'")
            with open("debug_output/allkeyshop_last_response.html", "w", encoding='utf-8') as f:
                f.write(response.text)
            logging.info("Allkeyshop'tan gelen HTML yanıtı 'debug_output/allkeyshop_last_response.html' dosyasına kaydedildi.")
            return None

    except Exception as e:
        logging.error(f"ALLKEYSHOP HATA: {e}", exc_info=True)
        return None


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
        # headless=False yaparak tarayıcıyı Replit'te VNC ile görebilirsiniz (debug için faydalı olabilir)
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
            "allkeyshop": asyncio.to_thread(get_allkeyshop_price, oyun_adi_temiz)
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        sonuclar = dict(zip(tasks.keys(), results))

        display_game_name = oyun_adi_orjinal
        steam_sonucu = sonuclar.get("steam")
        if isinstance(steam_sonucu, dict) and steam_sonucu.get("name"):
            display_game_name = steam_sonucu['name']

        embed = discord.Embed(title=f"🎮 {display_game_name} Fiyat Bilgisi ve Linkler V.0.33", color=discord.Color.from_rgb(16, 124, 16))
        embed.set_footer(text="Fiyatlar anlık olarak mağazalardan çekilmektedir.")

        # --- Sonuçları İşleme (Hata Kontrolü Eklendi) ---

        # Mağaza sırasını belirleyelim
        store_order = ["steam", "allkeyshop", "ps", "xbox", "epic"]

        for store in store_order:
            result = sonuclar.get(store)
            store_name = {
                "steam": "Steam", "allkeyshop": "Allkeyshop (CD-Key)",
                "ps": "PlayStation Store", "xbox": "Xbox Store",
                "epic": "Epic Games"
            }[store]

            # YENİ: Hata durumlarını ve boş sonuçları embed'e ekleme
            if isinstance(result, Exception):
                embed.add_field(name=store_name, value="`Hata oluştu.`", inline=True)
                logging.error(f"'{store}' deposu için sonuç işlenirken hata yakalandı: {result}", exc_info=result)
            elif result is None:
                 embed.add_field(name=store_name, value="`Bulunamadı.`", inline=True)
            elif store == "epic":
                embed.add_field(name=store_name, value=f"[Mağazada Ara]({result})", inline=True)
            else: # Başarılı sonuçlar
                price_info, link = result["price"], result["link"]
                display_text = ""
                if isinstance(price_info, tuple): # USD -> TRY çevirimi gerekenler
                    price, currency = price_info
                    try_rate = get_usd_to_try_rate()
                    if try_rate and currency == "USD":
                        tl_price = price * try_rate
                        display_text = f"${price:,.2f} {currency}\n(≈ {tl_price:,.2f} TL)"
                    else: 
                        display_text = f"${price:,.2f} {currency}"
                else: # Diğerleri (string fiyat bilgisi)
                    display_text = price_info

                embed.add_field(name=store_name, value=f"[{display_text}]({link})", inline=True)


        await msg.edit(content=None, embed=embed)

# --- Botu ve Sunucuyu Başlatma ---
# keep_alive() # Gerekliyse yorum satırını kaldırın
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
if DISCORD_TOKEN:
    client.run(DISCORD_TOKEN)
else:
    logging.critical("HATA: DISCORD_TOKEN .env dosyasında bulunamadı.")