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

# --- Oyun Adı Temizleme Fonksiyonu (GÜNCELLENDİ: Özel Karakterleri Boşlukla Değiştirme) ---
def clean_game_name(game_name):
    # Romen rakamlarını sayılara çevir, orijinal metni koru
    name_with_arabic, _ = clean_and_extract_roman(game_name)

    # 1. Adım: Özel karakterleri (harf, rakam veya boşluk olmayan her şeyi) boşlukla değiştir.
    # Bu, 'Cry®3' gibi ifadelerin 'Cry 3' olmasını sağlar.
    cleaned_name = re.sub(r'[^\w\s]', ' ', name_with_arabic, flags=re.UNICODE)

    # 2. Adım: Oluşabilecek çoklu boşlukları tek boşluğa indir.
    cleaned_name = re.sub(r'\s+', ' ', cleaned_name)

    return cleaned_name.strip().lower()

# --- YENİ: Romen Rakamı ve Sayı Çıkarma Yardımcıları ---
def clean_and_extract_roman(name):
    """Converts Roman numerals at the end of a string to Arabic numerals."""
    name = name.upper()
    roman_map = {'I': 1, 'V': 5, 'X': 10}
    replacements = {'IV': '4', 'IX': '9', 'V': '5', 'I': '1'} # Order matters

    # Check for specific roman numerals at the end
    if name.endswith(" IV"): return name.replace(" IV", " 4"), 4
    if name.endswith(" IX"): return name.replace(" IX", " 9"), 9
    if name.endswith(" V"): return name.replace(" V", " 5"), 5
    if name.endswith(" III"): return name.replace(" III", " 3"), 3
    if name.endswith(" II"): return name.replace(" II", " 2"), 2
    if name.endswith(" I"): return name.replace(" I", " 1"), 1

    return name.lower(), None # Return original cleaned name if no roman numeral

def extract_numbers_from_title(title):
    """Extracts all Arabic and Roman numerals from a game title."""
    # First, find all standard digits
    numbers = set(map(int, re.findall(r'\d+', title)))

    # Then, check for Roman numerals which are often used for sequels
    # We check for them as standalone words to avoid matching 'I' in 'is'
    # Using uppercase for consistency
    title_upper = f" {title.upper()} "
    if " II " in title_upper or title_upper.endswith(" II"): numbers.add(2)
    if " III " in title_upper or title_upper.endswith(" III"): numbers.add(3)
    if " IV " in title_upper or title_upper.endswith(" IV"): numbers.add(4)
    if " V " in title_upper or title_upper.endswith(" V"): numbers.add(5)

    return numbers

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

# --- Steam Fiyat ve Link Alma Fonksiyonu (YENİ: Akıllı Puanlama Sistemiyle) ---
def get_steam_price(game_name):
    try:
        # 1. Kullanıcının arama terimindeki sayıyı bul
        user_query_numbers = extract_numbers_from_title(game_name)
        # Eğer kullanıcı 'Red Dead Redemption' yazdıysa bu set boş olacak.
        # Eğer 'Red Dead Redemption 2' yazdıysa {2} olacak.

        search_url = f"https://store.steampowered.com/api/storesearch/?term={requests.utils.quote(game_name)}&l=turkish&cc=TR"
        response = requests.get(search_url)
        if response.status_code != 200 or not response.json().get('items'):
            logging.warning(f"Steam araması başarısız oldu. Status Code: {response.status_code}, Game: {game_name}")
            return None

        search_results = response.json().get('items', [])
        if not search_results:
            logging.info(f"Steam'de '{game_name}' için sonuç bulunamadı.")
            return None

        best_match = None
        highest_score = -1

        # 2. Tüm sonuçları gez ve puanla
        for item in search_results:
            item_name = item.get('name', '')
            cleaned_item_name = clean_game_name(item_name)

            # Puanlama Başlangıcı
            current_score = 0

            # Metinsel Benzerlik Puanı (Temel Puan)
            # Bu, "Bioshock" ile "Bioshock Remastered" eşleşmesini sağlar.
            # rapidfuzz kütüphanesi bu iş için harikadır ama basit bir `in` kontrolü de iş görür.
            # Daha basit ve hatasız olması için `in` kullanalım.
            if game_name in cleaned_item_name:
                current_score += 90
            elif cleaned_item_name in game_name:
                current_score += 85
            else: # Eğer temel isim bile eşleşmiyorsa, bu sonucu atla
                continue

            # Sayısal Eşleşme Puanı (Filtreleme)
            result_numbers = extract_numbers_from_title(cleaned_item_name)

            if user_query_numbers: # Kullanıcı bir sayı belirtti (örn: RDR 2)
                if not user_query_numbers.intersection(result_numbers):
                    current_score -= 100 # Yanlış devam oyunu, puanı düşürerek ele
            else: # Kullanıcı sayı belirtmedi (örn: RDR)
                # Sonuçta 1'den büyük bir sayı varsa (örn: RDR 2), bu istenmeyen bir devam oyunudur.
                if any(n > 1 for n in result_numbers):
                    current_score -= 100 # İstenmeyen devam oyunu, puanı düşürerek ele

            # En yüksek skorlu sonucu sakla
            if current_score > highest_score:
                highest_score = current_score
                best_match = item

        # 3. Yeterince iyi bir eşleşme bulunduysa devam et
        if not best_match or highest_score < 50:
             logging.info(f"Steam'de '{game_name}' için yeterli doğrulukta bir eşleşme bulunamadı.")
             return None

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


# --- PlayStation Store Fiyat ve Link Alma Fonksiyonu (YENİ: Doğrudan Arama Sonucundan Veri Çekme) ---
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

        # Olası cookie/pop-up'ları önceden ele almak için bir kerelik bekleme
        await page.goto(search_url, wait_until='domcontentloaded')

        try:
            # Cookie banner'ını veya diğer pop-up'ları arayıp tıkla
            cookie_button = page.locator('button:has-text("Accept All Cookies"), button:has-text("Tümünü Kabul Et")')
            if await cookie_button.count() > 0:
                logging.info("Cookie banner'ı bulundu ve tıklandı.")
                await cookie_button.first.click(timeout=5000)
                # Tıkladıktan sonra sonuçların yüklenmesi için kısa bir bekleme
                await page.wait_for_timeout(2000)
        except Exception:
            logging.info("Cookie banner'ı bulunamadı veya tıklanamadı, devam ediliyor.")

        results_selector = 'div[data-qa^="search#productTile"]'
        await page.wait_for_selector(results_selector, timeout=20000)

        all_results = await page.locator(results_selector).all()
        if not all_results:
            await page.close(); return None

        # Puanlama ile en iyi eşleşmeyi bulma...
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
                length_penalty = len(cleaned_item_name) - len(game_name)
                current_score -= length_penalty

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

        # --- YENİ MANTIK: Veriyi doğrudan bulunan karttan çek ---
        price_info = "Fiyat bilgisi yok."
        subscriptions = []

        # Kartın içindeki metnin tamamını al
        card_text = await best_match_element.inner_text()

        # Fiyatı ara (örn: "1.399,00 TL")
        price_match = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2}\s*TL)', card_text)
        if price_match:
            price_info = price_match.group(1)

        # Abonelikleri ara
        if "Extra" in card_text or "Premium" in card_text:
            subscriptions.append("PS Plus'a Dahil")
        if "GTA+" in card_text:
            subscriptions.append("GTA+'a Dahil")
        if "EA Play" in card_text:
            subscriptions.append("EA Play'e Dahil")

        # Link'i al
        link_element = best_match_element.locator('a.psw-link').first
        href = await link_element.get_attribute('href')
        link = "https://store.playstation.com" + href

        # Sonuçları Birleştir
        final_display_text = price_info
        if subscriptions:
            # Eğer bir abonelik varsa ama fiyat bulunamadıysa, fiyat yerine "Dahil" yazabiliriz.
            if final_display_text == "Fiyat bilgisi yok.":
                final_display_text = "Dahil"

            subscription_text = "\n*" + " & ".join(sorted(subscriptions)) + "*"
            # Eğer fiyat zaten Dahil ise, tekrar ekleme yapma
            if "Dahil" in final_display_text:
                 final_display_text = "*" + " & ".join(sorted(subscriptions)) + "*"
            else:
                 final_display_text = (final_display_text + subscription_text).strip()

        await page.close()
        return {"price": final_display_text, "link": link}

    except Exception as e:
        logging.error(f"PLAYSTATION HATA: {e}", exc_info=True)
        await take_screenshot_on_error(page, "playstation", game_name)
        if page and not page.is_closed(): await page.close()
        return None

# --- Xbox Store Fiyat ve Link Alma Fonksiyonu (GÜNCELLENDİ: GTA+ Kontrolü Eklendi) ---
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

        all_results = await page.query_selector_all('a[class*="commonStyles-module__basicButton"]')
        if not all_results:
            await page.close()
            return None

        user_query_numbers = extract_numbers_from_title(game_name_clean)
        best_match_element = None
        highest_score = -1

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
                highest_score = current_score
                best_match_element = result

        if not best_match_element or highest_score < 50:
            logging.warning(f"Xbox'da '{game_name_clean}' için yeterli doğrulukta eşleşme bulunamadı.")
            await page.close()
            return None

        await best_match_element.click()
        await page.wait_for_load_state('networkidle')
        link = page.url

        price_info = "Fiyat bilgisi yok."
        subscriptions = []

        # --- YENİ: Tüm Abonelikleri Kontrol Etme ---
        # 1. Game Pass kontrolü
        game_pass_selector = 'svg[aria-label="Game Pass ile birlikte gelir"]'
        if await page.locator(game_pass_selector).count() > 0:
            subscriptions.append("Game Pass'e Dahil")

        # 2. GTA+ kontrolü
        # Sayfada "GTA+ ile birlikte gelir" gibi bir metin arıyoruz.
        if await page.locator('*:has-text("GTA+")').count() > 0:
            # Emin olmak için daha spesifik bir metin arayabiliriz
            gta_plus_text_count = await page.locator('*:has-text("GTA+ ile birlikte gelir")').count()
            if gta_plus_text_count > 0 and "GTA+ ile birlikte gelir" not in subscriptions:
                 subscriptions.append("GTA+ ile birlikte gelir")

        # 3. Fiyat bilgisini al
        price_selector = 'span[class*="Price-module__boldText"]'
        price_element = page.locator(price_selector).first
        if await price_element.count() > 0:
            price_text = await price_element.inner_text()
            price_info = price_text

        await page.close()

        # 4. Sonuçları birleştir
        final_display_text = price_info
        if subscriptions:
            # Fiyatı "Dahil" gibi bir şeyle değiştirmemek için kontrol
            if final_display_text == "Fiyat bilgisi yok.":
                 final_display_text = "" # Fiyat yoksa boş bırak, sadece abonelik görünsün

            subscription_text = "\n*" + " & ".join(subscriptions) + "*"
            final_display_text = (final_display_text + subscription_text).strip()

        return {"price": final_display_text, "link": link}

    except Exception as e:
        logging.error(f"XBOX HATA: {e}", exc_info=True)
        await take_screenshot_on_error(page, "xbox", game_name_clean)
        if page and not page.is_closed(): await page.close()
        return None

# --- Allkeyshop Fiyat ve Link Alma Fonksiyonu (GÜNCELLENDİ: Başarısızlık Durumunda Arama Linki) ---
async def get_allkeyshop_price(game_name):
    global browser
    if not browser or not browser.is_connected():
        logging.warning("Allkeyshop fiyatı alınamıyor: Tarayıcı bağlı değil.")
        return None

    page = None
    try:
        formatted_game_name_url = game_name.replace(' ', '-')

        # İki farklı URL formatını tanımlıyoruz
        url_pattern_1 = f"https://www.allkeyshop.com/blog/en-us/buy-{formatted_game_name_url}-cd-key-compare-prices/"
        url_pattern_2 = f"https://www.allkeyshop.com/blog/en-us/compare-and-buy-cd-key-for-digital-download-{formatted_game_name_url}/"
        urls_to_try = [url_pattern_1, url_pattern_2]

        page = await browser.new_page()

        # URL listesini denemek için bir döngü oluşturuyoruz
        for i, url in enumerate(urls_to_try):
            logging.info(f"Allkeyshop için gidiliyor (Deneme {i+1}): {url}")
            try:
                # Timeout süresini kısa tutarak başarısızlık durumunda hızlıca diğer adıma geçmesini sağlıyoruz.
                await page.goto(url, timeout=10000, wait_until='domcontentloaded')

                html_content = await page.content()
                pattern = re.search(r"var gamePageTrans = ({.*?});", html_content, re.DOTALL)

                if not pattern:
                    logging.warning(f"URL denemesi {i+1} başarısız: 'gamePageTrans' bloğu bulunamadı.")
                    continue

                json_data_str = pattern.group(1)
                data = json.loads(json_data_str)
                prices_list = data.get("prices")

                if not prices_list or not isinstance(prices_list, list):
                    logging.warning(f"URL denemesi {i+1} başarısız: JSON içinde 'prices' listesi yok.")
                    continue

                key_offers = [offer for offer in prices_list if offer.get('account') is False and 'priceCard' in offer]

                if not key_offers:
                    logging.warning(f"URL denemesi {i+1} başarılı, ancak anahtar (key) teklifi bulunamadı.")
                    continue # Diğer URL'i deneyebiliriz.

                lowest_price = min(float(offer['priceCard']) for offer in key_offers)
                logging.info(f"Allkeyshop için en düşük KREDİ KARTI DAHİL fiyat bulundu: {lowest_price} USD (URL: {url})")

                # Başarılı olunca sonucu döndür ve fonksiyondan çık.
                return {"price": (lowest_price, "USD"), "link": url}

            except Exception as e:
                logging.warning(f"URL denemesi {i+1} sırasında hata: {e}")
                continue # Hata durumunda bir sonraki URL'i dene

        # --- YENİ MANTIK: Eğer döngü biterse ve hiçbir URL çalışmazsa ---
        # Oyun bulunamamıştır. Bu durumda, genel bir arama linki oluştur.
        logging.warning(f"Allkeyshop'ta '{game_name}' için veri çekilemedi. Arama linki oluşturuluyor.")
        
        # Oyun adını URL için güvenli hale getiriyoruz.
        search_query = requests.utils.quote(game_name)
        # Allkeyshop'un arama URL formatını kullanıyoruz.
        search_url = f"https://www.allkeyshop.com/blog/search/{search_query}/"
        
        # Ana mesaj döngüsünün bunu doğru işlemesi için özel bir "price" metni ve linki içeren bir sözlük döndürüyoruz.
        return {"price": "Mağazada Ara", "link": search_url}

    except Exception as e:
        logging.error(f"ALLKEYSHOP (Playwright) GENEL HATA: {e}", exc_info=False)
        if page:
            await take_screenshot_on_error(page, "allkeyshop", game_name)
        return None # Genel bir hata durumunda hala None dönebiliriz.
    finally:
        if page and not page.is_closed():
            await page.close()


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
            "allkeyshop": get_allkeyshop_price(oyun_adi_temiz)
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        sonuclar = dict(zip(tasks.keys(), results))

        display_game_name = oyun_adi_orjinal
        steam_sonucu = sonuclar.get("steam")
        if isinstance(steam_sonucu, dict) and steam_sonucu.get("name"):
            display_game_name = steam_sonucu['name']

        embed = discord.Embed(title=f"🎮 {display_game_name} Fiyat Bilgisi ve Linkler V.0.51", color=discord.Color.from_rgb(16, 124, 16))
        embed.set_footer(text="Fiyatlar anlık olarak mağazalardan çekilmektedir.")

        # --- Sonuçları İşleme (Hata Kontrolü Eklendi) ---

        # Mağaza sırasını belirleyelim
        store_order = ["steam", "xbox", "ps", "epic", "allkeyshop"]

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