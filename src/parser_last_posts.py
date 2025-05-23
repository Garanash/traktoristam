import asyncio
import aiohttp
import re
from datetime import datetime, timedelta
from collections import deque
from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetHistoryRequest
from bs4 import BeautifulSoup
import config


class ChannelMonitor:
    def __init__(self):
        self.last_checked_id = 0
        self.processed_ids = set()
        self.client = None
        self.output_channel = None
        self.private_channel = None
        self.message_queue = deque()
        self.current_message_processing = None
        self.article_timeouts = {}
        self.pending_responses = {}
        self.api_request_lock = asyncio.Lock()
        self.processing_lock = asyncio.Lock()
        self.processing_active = False
        self.autopiter_session = aiohttp.ClientSession()

    async def initialize(self):
        """Инициализация клиента Telegram и каналов"""
        self.client = TelegramClient('session_name', config.API_ID, config.API_HASH)
        await self.client.start(config.PHONE_NUMBER)

        try:
            self.output_channel = await self.client.get_entity(config.OUTPUT_CHANNEL_ID)
            self.private_channel = await self.client.get_entity(config.PRIVATE_CHANNEL_ID)

            print("✅ Бот инициализирован")

            output_name = getattr(self.output_channel, 'title',
                                  getattr(self.output_channel, 'username',
                                          getattr(self.output_channel, 'first_name', 'N/A')))
            private_name = getattr(self.private_channel, 'title',
                                   getattr(self.private_channel, 'username',
                                           getattr(self.private_channel, 'first_name', 'N/A')))

            print(f"📢 Выходной канал: {output_name}")
            print(f"🔒 Приватный канал: {private_name}")

        except Exception as e:
            print(f"⚠️ Ошибка инициализации каналов: {e}")
            raise

    async def fetch_autopiter_price(self, article):
        """Парсинг цены с Autopiter"""
        url = f"https://autopiter.ru/goods/{article}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        try:
            async with self.autopiter_session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')

                    # Ищем товар с точным совпадением артикула
                    for item in soup.select('ul.goods-list li.goods-list__item'):
                        part_number = item.select_one('div.goods-list__info div.goods-list__article p')
                        if part_number and article.lower() in part_number.get_text().lower():
                            price_element = item.select_one('div.goods-list__price span.price__value')
                            if price_element:
                                price_text = price_element.get_text().strip()
                                price = float(re.sub(r'[^\d.]', '', price_text.replace(',', '.')))
                                return price
                    return None
                return None
        except Exception as e:
            print(f"⚠️ Ошибка парсинга Autopiter: {e}")
            return None

    async def extract_articles_with_perplexity(self, message_text):
        """Запрос к Perplexity API для извлечения артикулов"""
        url = "https://api.perplexity.ai/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.PERPLEXITY_API_KEY}",
            "Content-Type": "application/json"
        }

        system_prompt = """Ты помогаешь извлекать артикулы товаров. Правила:
1. Найди все артикулы (комбинации букв, цифр и дефисов)
2. Определи количество для каждого (по умолчанию 1)
3. Выведи строго в формате: артикул: количество
4. Только факты, без комментариев!"""

        payload = {
            "model": "sonar-pro",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Извлеки артикулы:\n{message_text}"}
            ],
            "temperature": 0.1,
            "max_tokens": 1000
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30)
                ) as response:

                    if response.status == 200:
                        data = await response.json()
                        content = data['choices'][0]['message']['content']

                        result = []
                        for line in content.split('\n'):
                            line = line.strip()
                            if re.match(r'^[\w\d-]+:\s*\d+$', line):
                                result.append(line)

                        return {'done': '\n'.join(result)} if result else None

                    print(f"⚠️ Ошибка API (статус {response.status}): {await response.text()}")
                    return None

        except Exception as e:
            print(f"⚠️ Ошибка запроса к Perplexity: {str(e)}")
            return None

    async def process_message_queue(self):
        """Обработка очереди сообщений"""
        while True:
            try:
                if self.message_queue and not self.processing_active:
                    async with self.processing_lock:
                        if self.message_queue and not self.processing_active:
                            self.processing_active = True
                            message_data = self.message_queue.popleft()

                            print(f"\n🚀 Начата обработка сообщения ID: {message_data['message'].id}")

                            processing_data = {
                                'message': message_data['message'],
                                'user': message_data['user'],
                                'articles_data': [],
                                'responses': [],
                                'timestamp': datetime.now(),
                                'pending_articles': set(),
                                'total_articles': 0
                            }

                            api_response = await self.extract_articles_with_perplexity(
                                message_data['message'].message
                            )

                            if api_response:
                                await self.process_api_response(processing_data, api_response)

                                # Запоминаем общее количество артикулов
                                processing_data['total_articles'] = len(processing_data['articles_data'])

                                for article in processing_data['articles_data']:
                                    await self.client.send_message(
                                        self.output_channel,
                                        article['article']
                                    )
                                    self.article_timeouts[article['article']] = datetime.now()
                                    processing_data['pending_articles'].add(article['article'])
                                    self.pending_responses[article['article']] = processing_data

                                    # Получаем цену с Autopiter
                                    autopiter_price = await self.fetch_autopiter_price(article['article'])
                                    if autopiter_price:
                                        article['autopiter_price'] = autopiter_price

                                self.current_message_processing = processing_data
                            else:
                                print("❌ Не удалось обработать сообщение")
                                self.processing_active = False
                await asyncio.sleep(1)
            except Exception as e:
                print(f"⚠️ Ошибка в process_message_queue: {e}")
                self.processing_active = False
                await asyncio.sleep(5)

    async def process_api_response(self, processing_data, api_response):
        """Обработка ответа от API"""
        if not api_response or 'done' not in api_response:
            return

        for line in api_response['done'].split('\n'):
            try:
                article, quantity = map(str.strip, line.split(':'))
                processing_data['articles_data'].append({
                    'article': article,
                    'quantity': float(quantity),
                    'processed': False,
                    'found': None,
                    'response_data': None,
                    'autopiter_price': None
                })
                print(f"🔍 Найден артикул: {article} ({quantity} шт.)")
            except Exception as e:
                print(f"⚠️ Ошибка обработки строки: {line}")

    async def handle_output_channel_response(self, response_message):
        """Обработка ответа от второго бота с ценами"""
        try:
            if not response_message.out and response_message.sender_id == self.output_channel.id:
                bot_data = self.parse_second_bot_response(response_message.message)
                if not bot_data:
                    return

                found_articles = set()
                for article in list(self.pending_responses.keys()):
                    if article in response_message.message:
                        processing_data = self.pending_responses.get(article)
                        if not processing_data:
                            continue

                        for article_data in processing_data['articles_data']:
                            if article_data['article'] == article and not article_data['processed']:
                                article_data.update({
                                    'processed': True,
                                    'found': bot_data['found'],
                                    'response_data': bot_data
                                })

                                if bot_data['found']:
                                    processing_data['responses'].append({
                                        'article_data': article_data,
                                        'bot_data': bot_data
                                    })
                                    print(f"💰 Получена цена для {article}")

                                found_articles.add(article)
                                processing_data['pending_articles'].discard(article)

                                # Проверяем, все ли артикулы обработаны
                                if (len(processing_data['responses']) +
                                    sum(1 for a in processing_data['articles_data']
                                        if a['processed'] and not a['found'])) == processing_data['total_articles']:
                                    await self.finalize_processing(processing_data)
                                    return

                for article in found_articles:
                    if article in self.pending_responses:
                        del self.pending_responses[article]
                    if article in self.article_timeouts:
                        del self.article_timeouts[article]
        except Exception as e:
            print(f"⚠️ Ошибка в handle_output_channel_response: {e}")

    def parse_second_bot_response(self, response_text):
        """Парсинг ответа от второго бота"""
        result = {
            'name': None,
            'price': None,
            'stock_quantity': None,
            'found': False
        }

        try:
            if "Артикул не найден" not in response_text:
                if name_match := re.search(r'Наименование:\s*(.*)', response_text):
                    result['name'] = name_match.group(1).strip()
                if price_match := re.search(r'Цена за штуку:\s*([\d.,]+)', response_text):
                    result['price'] = float(price_match.group(1).replace(',', '.'))
                    result['found'] = True
                if stock_match := re.search(r'Количество на складе:\s*(\d+)', response_text):
                    result['stock_quantity'] = int(stock_match.group(1))
        except Exception as e:
            print(f"⚠️ Ошибка парсинга: {e}")

        return result

    async def check_timeouts(self):
        """Проверка таймаутов обработки артикулов"""
        while True:
            try:
                now = datetime.now()
                timeout = timedelta(seconds=60)

                for article, timestamp in list(self.article_timeouts.items()):
                    if (now - timestamp) > timeout:
                        processing_data = self.pending_responses.get(article)
                        if processing_data:
                            for article_data in processing_data['articles_data']:
                                if article_data['article'] == article and not article_data['processed']:
                                    article_data.update({
                                        'processed': True,
                                        'found': False,
                                        'response_data': None
                                    })
                                    print(f"⏰ Таймаут для артикула {article}")

                            processing_data['pending_articles'].discard(article)
                            if article in self.pending_responses:
                                del self.pending_responses[article]
                            if article in self.article_timeouts:
                                del self.article_timeouts[article]

                            # Проверяем, все ли артикулы обработаны
                            if (len(processing_data['responses']) +
                                sum(1 for a in processing_data['articles_data']
                                    if a['processed'] and not a['found'])) == processing_data['total_articles']:
                                await self.finalize_processing(processing_data)
                await asyncio.sleep(5)
            except Exception as e:
                print(f"⚠️ Ошибка в check_timeouts: {e}")
                await asyncio.sleep(10)

    async def finalize_processing(self, processing_data):
        """Финальная обработка и отправка результата"""
        try:
            if not processing_data:
                return

            priced_items = [
                r for r in processing_data['responses']
                if r['article_data']['found']
            ]

            if priced_items or any(a.get('autopiter_price') for a in processing_data['articles_data']):
                await self.send_to_private_channel(processing_data)
            else:
                print("ℹ️ Нет расцененных артикулов")

            if processing_data == self.current_message_processing:
                self.cleanup_processing()
        except Exception as e:
            print(f"⚠️ Ошибка в finalize_processing: {e}")
            self.cleanup_processing()

    async def send_to_private_channel(self, processing_data):
        """Формирование и отправка результата в приватный канал"""
        try:
            original_message = processing_data['message']
            channel_entity = await self.client.get_entity(original_message.peer_id)
            post_link = f"https://t.me/c/{channel_entity.id}/{original_message.id}"

            response_text = f"📎 [Исходное сообщение]({post_link})\n"
            response_text += "📊 Результат обработки артикулов:\n"

            total_sum = 0
            total_discount_sum = 0

            for article_data in processing_data['articles_data']:
                if article_data['found'] or article_data.get('autopiter_price'):
                    response_text += f"\n🔹 Артикул: {article_data['article']}\n"
                    response_text += f"📦 Запрошенное количество: {int(article_data['quantity'])}\n"

                    if article_data['found']:
                        bot_data = article_data['response_data']
                        item_total = bot_data['price'] * article_data['quantity']
                        item_discount = item_total * 0.97

                        total_sum += item_total
                        total_discount_sum += item_discount

                        stock_info = f" ({bot_data['stock_quantity']} шт на складе)" if bot_data[
                            'stock_quantity'] else ""

                        response_text += (
                            f"🏷️ Наименование: {bot_data['name'] or 'Без названия'}{stock_info}\n"
                            f"💰 Цена за штуку: {bot_data['price']:.2f} ₽/шт\n"
                        )

                    if article_data.get('autopiter_price'):
                        response_text += f"🛒 Цена на Autopiter: {article_data['autopiter_price']:.2f} ₽/шт\n"

            if total_sum > 0:
                response_text += (
                    f"\n💵 Общая сумма: {total_sum:.2f} ₽\n"
                )

            await self.client.send_message(
                entity=self.private_channel,
                message=response_text,
                link_preview=False
            )

            print(f"✉️ Результат отправлен в приватный канал")
        except Exception as e:
            print(f"⚠️ Ошибка при отправке в приватный канал: {e}")

    def cleanup_processing(self):
        """Очистка данных после обработки"""
        self.processing_active = False
        self.current_message_processing = None
        self.article_timeouts.clear()
        self.pending_responses.clear()

    async def monitor_output_channel(self):
        """Мониторинг ответов от второго бота"""
        print("👂 Начало мониторинга выходного канала...")

        @self.client.on(events.NewMessage(chats=self.output_channel))
        async def handler(event):
            try:
                await self.handle_output_channel_response(event.message)
            except Exception as e:
                print(f"⚠️ Ошибка обработки сообщения: {e}")

        while True:
            await asyncio.sleep(10)

    async def process_messages(self, user, messages):
        """Обработка новых сообщений из канала"""
        new_messages = [msg for msg in messages if msg.id > self.last_checked_id]

        if not new_messages:
            return

        new_messages.sort(key=lambda x: x.id)
        messages_to_process = new_messages[-10:]

        for message in messages_to_process:
            if not message.message:
                continue

            print(f"\n📥 Новое сообщение в очереди (ID: {message.id})")
            self.message_queue.append({
                'message': message,
                'user': user
            })

        self.last_checked_id = max(msg.id for msg in messages_to_process)

    async def close(self):
        """Корректное закрытие сессий"""
        await self.autopiter_session.close()
        await self.client.disconnect()


async def main():
    monitor = ChannelMonitor()
    await monitor.initialize()

    try:
        input_channel = await monitor.client.get_entity(config.CHANNEL_ID)
        user = await monitor.client.get_entity(config.USER_ID)

        print(f"\n🔍 Мониторинг канала: {input_channel.title}")
        print(f"👤 Пользователь: {user.first_name if user.first_name else user.username}\n")

        tasks = [
            asyncio.create_task(monitor.monitor_output_channel()),
            asyncio.create_task(monitor.check_timeouts()),
            asyncio.create_task(monitor.process_message_queue())
        ]

        while True:
            try:
                history = await monitor.client(GetHistoryRequest(
                    peer=input_channel,
                    limit=100,
                    offset_date=None,
                    offset_id=0,
                    max_id=0,
                    min_id=monitor.last_checked_id,
                    add_offset=0,
                    hash=0
                ))

                if history.messages:
                    await monitor.process_messages(user, history.messages)

                await asyncio.sleep(60)
            except Exception as e:
                print(f"⚠️ Ошибка: {e}")
                await asyncio.sleep(10)

    except Exception as e:
        print(f"🚨 Критическая ошибка: {e}")
    finally:
        await monitor.close()
        print("🔴 Бот остановлен")


if __name__ == '__main__':
    print("🟢 Запуск бота для обработки артикулов")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🔴 Бот остановлен пользователем")