import asyncio
import aiohttp
from telethon import TelegramClient
from telethon.tl.functions.messages import GetHistoryRequest
import config
import re
from datetime import datetime, timedelta
from collections import deque


class ChannelMonitor:
    def __init__(self):
        self.last_checked_id = 0
        self.processed_ids = set()
        self.client = None
        self.output_channel = None
        self.message_queue = deque()
        self.current_message_processing = None
        self.article_timeouts = {}
        self.pending_responses = set()
        self.api_request_lock = asyncio.Lock()
        self.processing_lock = asyncio.Lock()
        self.processing_active = False  # Флаг активности обработки

    async def initialize(self):
        self.client = TelegramClient('new_user_session', config.API_ID, config.API_HASH)
        await self.client.start(config.PHONE_NUMBER)

        if isinstance(config.OUTPUT_CHANNEL_ID, str):
            self.output_channel = await self.client.get_entity(config.OUTPUT_CHANNEL_ID)
        else:
            self.output_channel = await self.client.get_entity(int(config.OUTPUT_CHANNEL_ID))

        print("✅ Клиент успешно инициализирован")

    async def send_to_protalk_api(self, message_text, max_retries=3):
        url = "https://api.pro-talk.ru/api/v1.0/ask/k7LZ7GN49P1UtvN1DJX4qYesMXml82Ij"
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0'
        }

        # Убедимся, что message_text не содержит None
        clean_message = str(message_text) if message_text else ""

        payload = {
            "bot_id": 25815,
            "chat_id": "channel_monitor",
            "message": clean_message[:4096]  # Ограничим размер сообщения
        }

        print(f"🔄 Отправка в API: {clean_message[:50]}...")

        # Конфигурация таймаутов (в секундах)
        connector = aiohttp.TCPConnector(
            force_close=True,
            enable_cleanup_closed=True,
            limit=30
        )

        timeout = aiohttp.ClientTimeout(
            total=30,  # Общий таймаут
            connect=15,  # Таймаут на подключение
            sock_connect=15,  # Таймаут на установку соединения
            sock_read=15  # Таймаут на чтение
        )

        for attempt in range(1, max_retries + 1):
            try:
                async with aiohttp.ClientSession(
                        connector=connector,
                        timeout=timeout,
                        trust_env=True
                ) as session:
                    try:
                        async with session.post(
                                url,
                                json=payload,
                                headers=headers,
                                ssl=False  # Попробуйте True если False не работает
                        ) as response:

                            # Проверяем статус ответа
                            if response.status != 200:
                                error_text = await response.text()
                                print(f"⚠️ API Error {response.status}: {error_text}")
                                continue

                            try:
                                data = await response.json()
                                if 'done' not in data:
                                    print(f"⚠️ Invalid API response: {data}")
                                    continue

                                return data

                            except ValueError as e:
                                print(f"⚠️ JSON decode error: {e}")
                                continue

                    except asyncio.TimeoutError:
                        print(f"⌛ Timeout occurred (attempt {attempt}/{max_retries})")

                    except aiohttp.ClientError as e:
                        print(f"⚠️ Connection error (attempt {attempt}/{max_retries}): {str(e)}")

            except Exception as e:
                print(f"⚠️ Unexpected error (attempt {attempt}/{max_retries}): {str(e)}")

            # Экспоненциальная задержка между попытками
            if attempt < max_retries:
                wait_time = min(2 ** attempt, 10)  # Максимум 10 секунд
                print(f"⏳ Waiting {wait_time}s before retry...")
                await asyncio.sleep(wait_time)

        print(f"❌ Failed after {max_retries} attempts")
        return None

    async def extract_articles_with_perplexity(self, message_text, max_retries=3):
        """
        Анализирует текст сообщения и извлекает артикулы с количествами
        Возвращает список строк в формате "артикул: количество" или None при ошибке
        """
        url = "https://api.perplexity.ai/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.PERPLEXITY_API_KEY}",
            "Content-Type": "application/json"
        }

        system_prompt = """Ты помощник для извлечения артикулов товаров. Твоя задача:
    1. Найти в тексте все артикулы товаров (комбинации букв и цифр)
    2. Определить их количество (если указано)
    3. Вывести строго в формате: "артикул: количество"
    4. Если количество не указано - использовать 1
    5. Никаких дополнительных комментариев и текста!

    Пример вывода:
    11Y-60-28712: 1
    708-2Н-32210: 2
    04697239: 1"""

        payload = {
            "model": "sonar-pro",
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": f"Извлеки артикулы из текста:\n{message_text}"
                }
            ],
            "temperature": 0.1,  # Минимум креативности
            "max_tokens": 1000
        }

        print(f"🔍 Анализ текста через Perplexity: {message_text[:50]}...")

        async with aiohttp.ClientSession() as session:
            for attempt in range(1, max_retries + 1):
                try:
                    async with session.post(
                            url,
                            json=payload,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=30)
                    ) as response:

                        if response.status == 200:
                            data = await response.json()
                            response_text = data['choices'][0]['message']['content']

                            # Очистка и валидация результата
                            cleaned_lines = []
                            for line in response_text.split('\n'):
                                line = line.strip()
                                if ':' in line:
                                    # Удаляем все символы кроме разрешенных (буквы, цифры, дефисы, ":")
                                    clean_line = re.sub(r'[^\w\d\-:]', '', line)
                                    cleaned_lines.append(clean_line)

                            if cleaned_lines:
                                return {'done': '\n'.join(cleaned_lines)}
                            else:
                                print("⚠️ Не найдены артикулы в ответе")
                                return None

                        else:
                            error = await response.text()
                            print(f"⚠️ Ошибка API (статус {response.status}): {error}")

                except asyncio.TimeoutError:
                    print(f"⌛ Таймаут соединения (попытка {attempt}/{max_retries})")
                except Exception as e:
                    print(f"⚠️ Ошибка (попытка {attempt}/{max_retries}): {str(e)}")

                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        print(f"❌ Не удалось получить артикулы после {max_retries} попыток")
        return None





    def parse_second_bot_response(self, response_text):
        result = {
            'name': None,
            'price': None,
            'stock_quantity': None,
            'found': False
        }

        try:
            if "Артикул не найден" in response_text:
                return result

            name_match = re.search(r'Наименование:\s*(.*)', response_text)
            if name_match:
                result['name'] = name_match.group(1).strip()

            price_match = re.search(r'Цена за штуку:\s*([\d.,]+)', response_text)
            if price_match:
                result['price'] = float(price_match.group(1).replace(',', '.'))
                result['found'] = True

            stock_match = re.search(r'Количество на складе:\s*(\d+)', response_text)
            if stock_match:
                result['stock_quantity'] = int(stock_match.group(1))

        except Exception as e:
            print(f"⚠️ Ошибка парсинга ответа второго бота: {e}")

        return result

    async def process_message_queue(self):
        while True:
            try:
                if self.message_queue and not self.processing_active:
                    async with self.processing_lock:
                        if self.message_queue and not self.processing_active:
                            self.processing_active = True
                            message_data = self.message_queue.popleft()

                            print(f"🚀 Начата обработка сообщения ID: {message_data['message'].id}")

                            # Создаем новый объект для обработки
                            processing_data = {
                                'message': message_data['message'],
                                'user': message_data['user'],
                                'articles_data': [],
                                'responses': [],
                                'timestamp': datetime.now(),
                                'api_response': None
                            }

                            # Получаем ответ от API
                            # Было:
                            # api_response = await self.send_to_protalk_api(message_data['message'].message)

                            # Стало:
                            api_response = await self.extract_articles_with_perplexity(message_data['message'].message)
                            if not api_response:
                                print("❌ Не удалось получить ответ от API")
                                self.processing_active = False
                                continue

                            processing_data['api_response'] = api_response

                            # Обрабатываем ответ API
                            await self.process_api_response(processing_data)

                            # Если есть артикулы для обработки - отправляем их во второй канал
                            if processing_data['articles_data']:
                                self.current_message_processing = processing_data
                                for article in processing_data['articles_data']:
                                    await self.client.send_message(self.output_channel, article['article'])
                                    print(f"📤 Отправлен артикул во второй канал: {article['article']}")
                                    self.article_timeouts[article['article']] = datetime.now()
                                    self.pending_responses.add(article['article'])
                            else:
                                print("ℹ️ Нет артикулов для обработки")
                                self.processing_active = False

                await asyncio.sleep(1)
            except Exception as e:
                print(f"⚠️ Ошибка в process_message_queue: {e}")
                self.processing_active = False
                self.current_message_processing = None
                await asyncio.sleep(1)

    async def process_api_response(self, processing_data):
        try:
            api_response = processing_data['api_response']
            if not api_response or 'done' not in api_response:
                print("ℹ️ Пустой ответ от API или отсутствует поле 'done'")
                return

            response_text = api_response['done'].strip()
            if not response_text or response_text in ('...', '…'):
                print("ℹ️ Ответ от API не содержит полезной информации")
                return

            lines = [line.strip() for line in response_text.split('\n') if line.strip()]
            if not lines:
                print("ℹ️ Нет строк для обработки в ответе API")
                return

            for line in lines:
                if ':' not in line:
                    continue

                try:
                    article_part, quantity_part = line.split(':', 1)
                    article = article_part.strip()
                    quantity = quantity_part.strip().split()[0]
                    quantity = float(quantity)

                    processing_data['articles_data'].append({
                        'article': article,
                        'quantity': quantity,
                        'processed': False,
                        'found': None,
                        'response_received': False
                    })

                except Exception as e:
                    print(f"⚠️ Ошибка обработки строки '{line}': {e}")
                    continue

        except Exception as e:
            print(f"⚠️ Ошибка в process_api_response: {e}")

    async def handle_output_channel_response(self, response_message):
        if not self.processing_active or self.current_message_processing is None:
            return

        try:
            bot_data = self.parse_second_bot_response(response_message.message)

            for article_data in self.current_message_processing['articles_data']:
                if (article_data['article'] in response_message.message and
                        not article_data['processed'] and
                        article_data['article'] in self.pending_responses):

                    article_data['processed'] = True
                    article_data['found'] = bot_data['found']
                    article_data['response_received'] = True

                    if bot_data['found']:
                        self.current_message_processing['responses'].append({
                            'article_data': article_data,
                            'bot_data': bot_data
                        })

                    self.pending_responses.discard(article_data['article'])
                    if article_data['article'] in self.article_timeouts:
                        del self.article_timeouts[article_data['article']]

                    await self.check_complete_response()
                    return

        except Exception as e:
            print(f"⚠️ Ошибка обработки ответа второго бота: {e}")

    async def check_complete_response(self):
        if not self.processing_active or self.current_message_processing is None:
            return

        try:
            timeout_threshold = timedelta(seconds=60)
            current_time = datetime.now()
            all_processed = True

            for article in self.current_message_processing['articles_data']:
                if not article['processed']:
                    if article['article'] in self.article_timeouts:
                        if (current_time - self.article_timeouts[article['article']]) > timeout_threshold:
                            article['processed'] = True
                            article['found'] = False
                            article['response_received'] = True
                            self.pending_responses.discard(article['article'])
                            del self.article_timeouts[article['article']]
                        else:
                            all_processed = False
                    else:
                        all_processed = False

            if all_processed:
                await self.finalize_processing()
        except Exception as e:
            print(f"⚠️ Ошибка в check_complete_response: {e}")
            await self.finalize_processing()

    async def finalize_processing(self):
        if not self.processing_active or self.current_message_processing is None:
            return

        try:
            articles_data = self.current_message_processing['articles_data']

            if not articles_data:
                print("ℹ️ Нет артикулов для обработки")
                self.cleanup_processing()
                return

            has_priced_items = any(article.get('found', False) for article in articles_data)
            has_unpriced_items = any(not article.get('found', True) for article in articles_data)

            if has_priced_items:
                await self.send_combined_response()
            else:
                print("ℹ️ Нет расцененных артикулов - сообщение не отправляется")

            self.cleanup_processing()
        except Exception as e:
            print(f"⚠️ Ошибка в finalize_processing: {e}")
            self.cleanup_processing()

    async def send_combined_response(self):
        try:
            message_data = self.current_message_processing
            original_message = message_data['message']
            responses = message_data['responses']
            articles = message_data['articles_data']

            priced_items = []
            unpriced_articles = []

            for article in articles:
                if article.get('found', False):
                    response = next((r for r in responses if r['article_data']['article'] == article['article']), None)
                    if response:
                        priced_items.append(response)
                elif article.get('found', False) is False:
                    unpriced_articles.append(article['article'])

            channel_entity = await self.client.get_entity(original_message.peer_id)
            post_link = f"https://t.me/c/{channel_entity.id}/{original_message.id}"
            response_text = f"📎 [Ссылка на пост]({post_link})\n\n"

            total_sum = 0
            total_discount_sum = 0

            if priced_items:
                response_text += "✅ Расценены следующие артикулы:\n\n"

                for item in priced_items:
                    article_data = item['article_data']
                    bot_data = item['bot_data']

                    item_total = bot_data['price'] * article_data['quantity']
                    item_discount = item_total * 0.97

                    total_sum += item_total
                    total_discount_sum += item_discount

                    stock_info = f" ({bot_data['stock_quantity']} на складе)" if bot_data[
                                                                                     'stock_quantity'] is not None else ""

                    response_text += (
                        f"🔹 Артикул: {article_data['article']}\n"
                        f"🏷️ Наименование: {bot_data['name'] or 'Не указано'}\n"
                        f"📦 Запрошенное количество: {article_data['quantity']}{stock_info}\n"
                        f"💰 Цена за единицу: {bot_data['price']:.2f}\n"
                        f"🧮 Итого по позиции: {item_total:.2f}\n"
                        f"🎁 Со скидкой 3%: {item_discount:.2f}\n\n"
                    )

            if unpriced_articles and priced_items:
                response_text += "\n🚫 Не расценены следующие артикулы:\n"
                for article in unpriced_articles:
                    response_text += f"• {article}\n"
                response_text += "\n"

            if priced_items:
                response_text += (
                    f"💵 Общая сумма: {total_sum:.2f}\n"
                    f"💳 Общая сумма со скидкой: {total_discount_sum:.2f}\n"
                )

            if priced_items:
                await self.client.send_message(
                    config.USER_ID,
                    response_text,
                    reply_to=original_message,
                    link_preview=False
                )
                print(
                    f"✉️ Отправлен ответ: {len(priced_items)} расцененных, {len(unpriced_articles)} нерасцененных артикулов")

        except Exception as e:
            print(f"⚠️ Ошибка формирования ответа: {e}")

    def cleanup_processing(self):
        self.processing_active = False
        self.current_message_processing = None
        self.article_timeouts.clear()
        self.pending_responses.clear()

    async def check_timeouts(self):
        while True:
            try:
                if self.processing_active and self.current_message_processing:
                    await self.check_complete_response()
                await asyncio.sleep(5)
            except Exception as e:
                print(f"⚠️ Ошибка проверки таймаутов: {e}")
                await asyncio.sleep(10)

    async def monitor_output_channel(self):
        print("👂 Начало мониторинга второго канала...")
        last_id = 0

        while True:
            try:
                history = await self.client(GetHistoryRequest(
                    peer=self.output_channel,
                    limit=5,
                    offset_date=None,
                    offset_id=0,
                    max_id=0,
                    min_id=last_id,
                    add_offset=0,
                    hash=0
                ))

                if history.messages:
                    for msg in history.messages:
                        if msg.id > last_id and not msg.out:
                            await self.handle_output_channel_response(msg)
                            last_id = msg.id

                await asyncio.sleep(5)
            except Exception as e:
                print(f"⚠️ Ошибка мониторинга второго канала: {e}")
                await asyncio.sleep(10)

    async def process_messages(self, user, messages):
        new_messages = [msg for msg in messages if msg.id > self.last_checked_id]

        if not new_messages:
            return

        new_messages.sort(key=lambda x: x.id)
        messages_to_process = new_messages[-10:]

        for message in messages_to_process:
            if not message.message:
                continue

            print(f"📥 Добавлено в очередь сообщение ID: {message.id}")
            self.message_queue.append({
                'message': message,
                'user': user
            })

        self.last_checked_id = max(msg.id for msg in messages_to_process)


async def main():
    monitor = ChannelMonitor()
    await monitor.initialize()

    try:
        if isinstance(config.CHANNEL_ID, str):
            input_channel = await monitor.client.get_entity(config.CHANNEL_ID)
        else:
            input_channel = await monitor.client.get_entity(int(config.CHANNEL_ID))
        print(f"🔍 Мониторим канал: {input_channel.title}")

        if isinstance(config.USER_ID, str):
            if config.USER_ID.startswith('@'):
                user = await monitor.client.get_entity(config.USER_ID)
            else:
                try:
                    user = await monitor.client.get_entity(int(config.USER_ID))
                except ValueError:
                    user = await monitor.client.get_entity(config.USER_ID)
        else:
            user = await monitor.client.get_entity(int(config.USER_ID))
        print(f"👤 Получен пользователь: {user.first_name}")

        asyncio.create_task(monitor.monitor_output_channel())
        asyncio.create_task(monitor.check_timeouts())
        asyncio.create_task(monitor.process_message_queue())

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
                else:
                    print("ℹ️ Новых сообщений нет")

                await asyncio.sleep(60)
            except Exception as e:
                print(f"⚠️ Ошибка: {e}")
                await asyncio.sleep(10)

    except Exception as e:
        print(f"⚠️ Критическая ошибка: {e}")
    finally:
        await monitor.client.disconnect()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Бот остановлен")
