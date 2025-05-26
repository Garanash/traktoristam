import asyncio
import aiohttp
import re
from datetime import datetime, timedelta
from collections import deque
from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetHistoryRequest
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
        self.http_session = aiohttp.ClientSession()

    async def initialize(self):
        """Инициализация клиента Telegram"""
        self.client = TelegramClient('session_name', config.API_ID, config.API_HASH)
        await self.client.start(config.PHONE_NUMBER)

        try:
            self.output_channel = await self.client.get_entity(config.OUTPUT_CHANNEL_ID)
            self.private_channel = await self.client.get_entity(config.PRIVATE_CHANNEL_ID)
            print("✅ Бот инициализирован")
        except Exception as e:
            print(f"⚠️ Ошибка инициализации: {e}")
            raise

    async def fetch_autopiter_price(self, article):
        """Поиск цены на Autopiter через прямое обращение к сайту"""
        url = f"https://autopiter.ru/goods/{article}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        try:
            async with self.http_session.get(url, headers=headers, timeout=20) as response:
                if response.status == 200:
                    html = await response.text()
                    # Ищем первую таблицу с ценами
                    table_match = re.search(r'<table[^>]*class="price-table"[^>]*>(.*?)</table>', html, re.DOTALL)
                    if table_match:
                        table_html = table_match.group(1)
                        # Ищем первую строку в таблице
                        row_match = re.search(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)
                        if row_match:
                            row_html = row_match.group(1)
                            # Извлекаем цену и количество из первой строки
                            price_match = re.search(r'data-price="([\d.]+)"', row_html)
                            quantity_match = re.search(r'data-stock="(\d+)"', row_html)

                            price = float(price_match.group(1)) if price_match else None
                            quantity = int(quantity_match.group(1)) if quantity_match else None

                            if price:
                                print(f"✅ Цена для {article}: {price} руб (на складе: {quantity or 'нет данных'})")
                                return {'price': price, 'quantity': quantity}

                    print(f"⚠️ Не удалось найти цену на странице для {article}")
                    return None
                print(f"⚠️ Ошибка запроса к Autopiter: {response.status}")
                return None
        except Exception as e:
            print(f"⚠️ Ошибка запроса к Autopiter: {e}")
            return None

    async def extract_articles_with_perplexity(self, message_text):
        """Извлечение артикулов через Perplexity API"""
        url = "https://api.perplexity.ai/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.PERPLEXITY_API_KEY}",
            "Content-Type": "application/json"
        }

        system_prompt = """Ты — профессиональный поисковик артикулов автозапчастей. 
Правила извлечения:
1. Найди ВСЕ артикулы (комбинации букв, цифр и дефисов длиной от 7 символов)
2. Для каждого артикула укажи количество (по умолчанию 1)
3. Формат вывода строго: артикул:количество
4. Только факты, без пояснений!"""

        payload = {
            "model": "sonar-pro",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Извлеки артикулы из текста:\n{message_text}"}
            ],
            "temperature": 0.1,
            "max_tokens": 1000
        }

        try:
            async with self.http_session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=30
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    content = data['choices'][0]['message']['content']

                    # Парсим ответ от Perplexity
                    result = []
                    for line in content.split('\n'):
                        line = line.strip()
                        if re.match(r'^[\w\d-]{6,}:\s*\d+$', line):  # Проверка на минимальную длину 7 символов
                            result.append(line)

                    return {'done': '\n'.join(result)} if result else None

                print(f"⚠️ Ошибка API (статус {response.status}): {await response.text()}")
                return None

        except Exception as e:
            print(f"⚠️ Ошибка запроса к Perplexity: {str(e)}")
            return None

    async def process_api_response(self, processing_data, api_response):
        """Обработка ответа от API"""
        if not api_response or 'done' not in api_response:
            return

        for line in api_response['done'].split('\n'):
            try:
                article, quantity = map(str.strip, line.split(':'))
                if len(article) >= 7:  # Дополнительная проверка длины
                    processing_data['articles_data'].append({
                        'article': article,
                        'quantity': float(quantity),
                        'processed': False,
                        'found': None,
                        'response_data': None,
                        'autopiter_data': None,
                        'second_bot_checked': False,
                        'autopiter_checked': False
                    })
                    print(f"🔍 Найден артикул: {article} ({quantity} шт.)")
            except Exception as e:
                print(f"⚠️ Ошибка обработки строки: {line}")

    async def process_message_queue(self):
        """Обработка очереди сообщений"""
        while True:
            try:
                if self.message_queue and not self.processing_active:
                    async with self.processing_lock:
                        if self.message_queue and not self.processing_active:
                            self.processing_active = True
                            message_data = self.message_queue.popleft()

                            print(f"\n🚀 Обработка сообщения ID: {message_data['message'].id}")

                            processing_data = {
                                'message': message_data['message'],
                                'user': message_data['user'],
                                'articles_data': [],
                                'responses': [],
                                'timestamp': datetime.now(),
                                'pending_articles': set(),
                                'total_articles': 0,
                                'not_found_articles': [],
                                'all_checked': False
                            }

                            api_response = await self.extract_articles_with_perplexity(
                                message_data['message'].message
                            )

                            if api_response:
                                await self.process_api_response(processing_data, api_response)
                                processing_data['total_articles'] = len(processing_data['articles_data'])

                                # Отправляем все артикулы во второй бот
                                for article_data in processing_data['articles_data']:
                                    article = article_data['article']
                                    await self.client.send_message(
                                        self.output_channel,
                                        article
                                    )
                                    self.article_timeouts[article] = datetime.now()
                                    processing_data['pending_articles'].add(article)
                                    self.pending_responses[article] = processing_data

                                self.current_message_processing = processing_data
                            else:
                                self.processing_active = False
                await asyncio.sleep(1)
            except Exception as e:
                print(f"⚠️ Ошибка в process_message_queue: {e}")
                self.processing_active = False
                await asyncio.sleep(5)

    async def handle_output_channel_response(self, response_message):
        """Обработка ответа от второго бота"""
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
                                    'response_data': bot_data,
                                    'second_bot_checked': True
                                })

                                if bot_data['found']:
                                    processing_data['responses'].append({
                                        'article_data': article_data,
                                        'bot_data': bot_data
                                    })
                                    print(f"💰 Получена цена для {article} из второго бота")
                                else:
                                    processing_data['not_found_articles'].append(article)
                                    print(f"🔍 Артикул {article} не найден во втором боте")

                                found_articles.add(article)
                                processing_data['pending_articles'].discard(article)

                                # Проверяем, все ли артикулы обработаны вторым ботом
                                all_second_bot_checked = all(
                                    a['second_bot_checked'] for a in processing_data['articles_data']
                                )

                                if all_second_bot_checked and not processing_data['all_checked']:
                                    # Если все артикулы проверены вторым ботом, начинаем проверку в Autopiter
                                    processing_data['all_checked'] = True
                                    await self.check_autopiter_for_all(processing_data)

                for article in found_articles:
                    if article in self.pending_responses:
                        del self.pending_responses[article]
                    if article in self.article_timeouts:
                        del self.article_timeouts[article]
        except Exception as e:
            print(f"⚠️ Ошибка обработки ответа: {e}")

    async def check_autopiter_for_all(self, processing_data):
        """Проверка всех не найденных артикулов в Autopiter"""
        try:
            print("🔍 Начинаем проверку не найденных артикулов в Autopiter")

            # Проверяем только артикулы, которые не были найдены во втором боте
            articles_to_check = [
                a for a in processing_data['articles_data']
                if not a['found'] and a['autopiter_data'] is None
            ]

            for article_data in articles_to_check:
                article = article_data['article']
                autopiter_data = await self.fetch_autopiter_price(article)
                if autopiter_data is not None:
                    article_data['autopiter_data'] = autopiter_data
                    print(f"🛒 Найдена цена в Autopiter для {article}: {autopiter_data['price']} руб")
                else:
                    print(f"⚠️ Артикул {article} не найден в Autopiter")

                article_data['autopiter_checked'] = True

            # После проверки всех артикулов в Autopiter формируем итоговый отчет
            await self.finalize_processing(processing_data)
        except Exception as e:
            print(f"⚠️ Ошибка при проверке Autopiter: {e}")
            await self.finalize_processing(processing_data)

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
        """Проверка таймаутов"""
        while True:
            try:
                now = datetime.now()
                timeout = timedelta(seconds=10)

                for article, timestamp in list(self.article_timeouts.items()):
                    if (now - timestamp) > timeout:
                        processing_data = self.pending_responses.get(article)
                        if processing_data:
                            for article_data in processing_data['articles_data']:
                                if article_data['article'] == article and not article_data['processed']:
                                    article_data.update({
                                        'processed': True,
                                        'found': False,
                                        'response_data': None,
                                        'second_bot_checked': True
                                    })
                                    print(f"⏰ Таймаут для артикула {article}")
                                    processing_data['not_found_articles'].append(article)

                            processing_data['pending_articles'].discard(article)
                            if article in self.pending_responses:
                                del self.pending_responses[article]
                            if article in self.article_timeouts:
                                del self.article_timeouts[article]

                            # Проверяем, все ли артикулы обработаны вторым ботом
                            all_second_bot_checked = all(
                                a['second_bot_checked'] for a in processing_data['articles_data']
                            )

                            if all_second_bot_checked and not processing_data['all_checked']:
                                # Если все артикулы проверены вторым ботом, начинаем проверку в Autopiter
                                processing_data['all_checked'] = True
                                await self.check_autopiter_for_all(processing_data)
                await asyncio.sleep(5)
            except Exception as e:
                print(f"⚠️ Ошибка проверки таймаутов: {e}")
                await asyncio.sleep(10)

    async def finalize_processing(self, processing_data):
        """Завершение обработки"""
        try:
            if not processing_data:
                return

            # Проверяем, все ли артикулы проверены в Autopiter
            all_autopiter_checked = all(
                a['autopiter_checked'] for a in processing_data['articles_data']
                if not a['found']
            )

            if not all_autopiter_checked:
                print("⏳ Ожидаем завершения проверки в Autopiter")
                return

            priced_items = [
                r for r in processing_data['responses']
                if r['article_data']['found']
            ]

            autopiter_items = [
                a for a in processing_data['articles_data']
                if a.get('autopiter_data') is not None
            ]

            if priced_items or autopiter_items:
                await self.send_to_private_channel(processing_data)
            else:
                print("ℹ️ Нет расцененных артикулов")

            if processing_data == self.current_message_processing:
                self.cleanup_processing()
        except Exception as e:
            print(f"⚠️ Ошибка завершения обработки: {e}")
            self.cleanup_processing()

    async def send_to_private_channel(self, processing_data):
        """Отправка результата в приватный канал с ссылкой на исходное сообщение"""
        try:
            original_message = processing_data['message']

            # Формируем основное сообщение с результатами
            response_text = ""

            # Получаем ссылку на исходное сообщение
            try:
                channel = await self.client.get_entity(original_message.peer_id)
                message_link = f"https://t.me/c/{channel.id}/{original_message.id}"
                link_text = f"🔗 [Исходный запрос]({message_link})\n\n"
            except Exception as e:
                print(f"⚠️ Ошибка получения ссылки: {e}")
                link_text = "\n⚠️ Не удалось получить ссылку на исходное сообщение"

            # Добавляем ссылку в конец сообщения
            response_text += link_text

            total_sum = 0
            second_bot_total = 0
            autopiter_total = 0

            # Собираем информацию о найденных артикулах
            second_bot_items = [a for a in processing_data['articles_data'] if a['found']]
            if second_bot_items:
                response_text += "🌿 По этому запросу найдено:\n"
                for article_data in second_bot_items:
                    bot_data = article_data['response_data']
                    item_total = bot_data['price'] * article_data['quantity']
                    second_bot_total += item_total

                    response_text += f"\n🔹 Артикул: {article_data['article']}\n"
                    response_text += f"🏷️ Название: {bot_data['name'] or 'Нет данных'}\n"
                    response_text += f"📦 Запрошено: {int(article_data['quantity'])}"
                    if bot_data['stock_quantity']:
                        response_text += f" (в наличии: {bot_data['stock_quantity']})"
                    response_text += "\n"
                    response_text += (
                        f"💰 Цена: {bot_data['price']:.2f} ₽\n"
                        f"💵 Сумма: {item_total:.2f} ₽\n"
                    )

            # Собираем информацию о артикулах из Autopiter
            autopiter_items = [a for a in processing_data['articles_data']
                               if a.get('autopiter_data') and not a['found']]
            if autopiter_items:
                response_text += "\n🛒 Найдено в Autopiter:\n"
                for article_data in autopiter_items:
                    autopiter_data = article_data['autopiter_data']
                    item_total = autopiter_data['price'] * article_data['quantity']
                    autopiter_total += item_total

                    response_text += f"\n🔹 Артикул: {article_data['article']}\n"
                    response_text += f"📦 Количество: {int(article_data['quantity'])}"
                    if autopiter_data['quantity']:
                        response_text += f" (в наличии: {autopiter_data['quantity']})"
                    response_text += "\n"
                    response_text += (
                        f"💰 Цена: {autopiter_data['price']:.2f} ₽\n"
                        f"💵 Сумма: {item_total:.2f} ₽\n"
                    )

            # Добавляем не найденные артикулы
            not_found_items = [a for a in processing_data['articles_data']
                               if not a['found'] and not a.get('autopiter_data')]
            if not_found_items:
                response_text += "\n🔴 Не найдены:\n"
                response_text += "\n".join(f"▪️ {a['article']}" for a in not_found_items) + "\n"
            response_text += '\n'
            # Добавляем итоговые суммы
            total_sum = second_bot_total + autopiter_total
            if total_sum > 0:
                if autopiter_total > 0:
                    response_text += f"\n💵 Итого (Autopiter): {autopiter_total:.2f} ₽\n"
                response_text += f"💵 Общая сумма: {total_sum:.2f} ₽\n"



            # Пытаемся отправить как reply
            try:
                await self.client.send_message(
                    entity=self.private_channel,
                    message=response_text,
                    reply_to=original_message.id,
                    link_preview=True
                )
                print("✉️ Результат отправлен как reply с ссылкой")
            except Exception as reply_error:
                print(f"⚠️ Не удалось отправить как reply: {reply_error}")
                # Если не получилось, отправляем обычным сообщением
                await self.client.send_message(
                    entity=self.private_channel,
                    message=response_text,
                    link_preview=True
                )
                print("✉️ Результат отправлен обычным сообщением с ссылкой")

        except Exception as e:
            print(f"⚠️ Критическая ошибка при отправке: {e}")

    def cleanup_processing(self):
        """Очистка данных"""
        self.processing_active = False
        self.current_message_processing = None
        self.article_timeouts.clear()
        self.pending_responses.clear()

    async def monitor_output_channel(self):
        """Мониторинг ответов"""
        print("👂 Начало мониторинга...")

        @self.client.on(events.NewMessage(chats=self.output_channel))
        async def handler(event):
            try:
                await self.handle_output_channel_response(event.message)
            except Exception as e:
                print(f"⚠️ Ошибка обработки: {e}")

        while True:
            await asyncio.sleep(10)

    async def process_messages(self, user, messages):
        """Обработка новых сообщений"""
        new_messages = [msg for msg in messages if msg.id > self.last_checked_id]

        if not new_messages:
            return

        new_messages.sort(key=lambda x: x.id)
        messages_to_process = new_messages[-10:]

        for message in messages_to_process:
            if not message.message:
                continue

            print(f"\n📥 Новое сообщение (ID: {message.id})")
            self.message_queue.append({
                'message': message,
                'user': user
            })

        self.last_checked_id = max(msg.id for msg in messages_to_process)

    async def close(self):
        """Закрытие соединений"""
        await self.http_session.close()
        await self.client.disconnect()


async def main():
    monitor = ChannelMonitor()
    await monitor.initialize()

    try:
        input_channel = await monitor.client.get_entity(config.CHANNEL_ID)
        user = await monitor.client.get_entity(config.USER_ID)

        print(f"\n🔍 Мониторинг: {input_channel.title}")
        print(f"👤 Пользователь: {user.first_name or user.username}\n")

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
    print("🟢 Запуск бота")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🔴 Остановлен пользователем")
