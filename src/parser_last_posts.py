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
        self.pending_responses = set()
        self.api_request_lock = asyncio.Lock()
        self.processing_lock = asyncio.Lock()
        self.processing_active = False

    async def initialize(self):
        """Инициализация клиента Telegram и каналов"""
        self.client = TelegramClient('session_name', config.API_ID, config.API_HASH)
        await self.client.start(config.PHONE_NUMBER)

        try:
            # Получаем сущности каналов
            self.output_channel = await self.client.get_entity(config.OUTPUT_CHANNEL_ID)
            self.private_channel = await self.client.get_entity(config.PRIVATE_CHANNEL_ID)

            print("✅ Бот инициализирован")

            # Безопасное получение названий (для каналов и пользователей)
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
            "model": "sonar-medium-online",
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

                        # Валидация и очистка ответа
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
            if self.message_queue and not self.processing_active:
                async with self.processing_lock:
                    if self.message_queue and not self.processing_active:
                        self.processing_active = True
                        message_data = self.message_queue.popleft()

                        print(f"\n🚀 Начата обработка сообщения ID: {message_data['message'].id}")

                        # Подготовка данных для обработки
                        processing_data = {
                            'message': message_data['message'],
                            'user': message_data['user'],
                            'articles_data': [],
                            'responses': [],
                            'timestamp': datetime.now()
                        }

                        # Запрос к Perplexity API
                        api_response = await self.extract_articles_with_perplexity(
                            message_data['message'].message
                        )

                        if api_response:
                            await self.process_api_response(processing_data, api_response)

                            # Отправка артикулов во второй канал для получения цен
                            for article in processing_data['articles_data']:
                                await self.client.send_message(
                                    self.output_channel,
                                    article['article']
                                )
                                self.article_timeouts[article['article']] = datetime.now()
                                self.pending_responses.add(article['article'])

                            self.current_message_processing = processing_data
                        else:
                            print("❌ Не удалось обработать сообщение")
                            self.processing_active = False

            await asyncio.sleep(1)

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
                    'found': None
                })
                print(f"🔍 Найден артикул: {article} ({quantity} шт.)")
            except Exception as e:
                print(f"⚠️ Ошибка обработки строки: {line}")

    async def handle_output_channel_response(self, response_message):
        """Обработка ответа от второго бота с ценами"""
        if not self.processing_active or not self.current_message_processing:
            return

        bot_data = self.parse_second_bot_response(response_message.message)

        for article_data in self.current_message_processing['articles_data']:
            if (article_data['article'] in response_message.message
                    and not article_data['processed']):

                article_data.update({
                    'processed': True,
                    'found': bot_data['found'],
                    'response_received': True
                })

                if bot_data['found']:
                    self.current_message_processing['responses'].append({
                        'article_data': article_data,
                        'bot_data': bot_data
                    })
                    print(f"💰 Получена цена для {article_data['article']}")

                self.pending_responses.discard(article_data['article'])
                await self.check_complete_response()
                break

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

    async def check_complete_response(self):
        """Проверка завершения обработки всех артикулов"""
        if not self.processing_active:
            return

        timeout = timedelta(seconds=60)
        now = datetime.now()

        all_processed = all(
            article['processed'] or
            (article['article'] in self.article_timeouts and
             (now - self.article_timeouts[article['article']]) > timeout)
            for article in self.current_message_processing['articles_data']
        )

        if all_processed:
            await self.finalize_processing()

    async def finalize_processing(self):
        """Финальная обработка и отправка результата"""
        if not self.processing_active:
            return

        try:
            priced_items = [
                r for r in self.current_message_processing['responses']
                if r['article_data']['found']
            ]

            if priced_items:
                await self.send_to_private_channel()
            else:
                print("ℹ️ Нет расцененных артикулов")

        finally:
            self.cleanup_processing()

    async def send_to_private_channel(self):
        """Формирование и отправка результата в приватный канал"""
        try:
            message_data = self.current_message_processing
            original_message = message_data['message']

            # Получаем информацию о канале отправителя
            channel_entity = await self.client.get_entity(original_message.peer_id)
            post_link = f"https://t.me/c/{channel_entity.id}/{original_message.id}"

            # Создаем заголовок сообщения
            response_text = f"📎 [Исходное сообщение]({post_link})\n\n"
            response_text += "📊 Результат обработки артикулов:\n\n"

            # Рассчитываем суммы
            total_sum = 0
            total_discount_sum = 0

            for item in message_data['responses']:
                if item['article_data']['found']:
                    bot_data = item['bot_data']
                    article_data = item['article_data']

                    item_total = bot_data['price'] * article_data['quantity']
                    item_discount = item_total * 0.97

                    total_sum += item_total
                    total_discount_sum += item_discount

                    stock_info = f" ({bot_data['stock_quantity']} шт.)" if bot_data['stock_quantity'] else ""

                    response_text += (
                        f"🔹 Артикул: {article_data['article']}\n"
                        f"🏷️ Наименование: {bot_data['name'] or 'Без названия'}\n"
                        f"📦 Количество: {article_data['quantity']}{stock_info}\n"
                        f"💰 Цена: {bot_data['price']:.2f} ₽/шт\n"
                        f"🧮 Сумма: {item_total:.2f} ₽\n"
                        f"🎁 Со скидкой 3%: {item_discount:.2f} ₽\n\n"
                    )

            # Добавляем итоговую информацию
            if total_sum > 0:
                response_text += (
                    f"💵 Общая сумма: {total_sum:.2f} ₽\n"
                    f"💳 Со скидкой: {total_discount_sum:.2f} ₽\n"
                )

            # Отправляем сообщение в приватный канал
            await self.client.send_message(
                entity=self.private_channel,
                message=response_text,
                link_preview=False
            )

            print(f"✉️ Результат отправлен в приватный канал {self.private_channel.title}")

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
        last_id = 0

        while True:
            try:
                history = await self.client(GetHistoryRequest(
                    peer=self.output_channel,
                    limit=5,
                    offset_id=0,
                    min_id=last_id
                ))

                if history.messages:
                    for msg in history.messages:
                        if msg.id > last_id and not msg.out:
                            await self.handle_output_channel_response(msg)
                            last_id = msg.id

                await asyncio.sleep(5)
            except Exception as e:
                print(f"⚠️ Ошибка мониторинга канала: {e}")
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

    async def check_timeouts(self):
        """Проверка таймаутов обработки артикулов"""
        while True:
            try:
                if self.processing_active and self.current_message_processing:
                    await self.check_complete_response()
                await asyncio.sleep(5)
            except Exception as e:
                print(f"⚠️ Ошибка проверки таймаутов: {e}")
                await asyncio.sleep(10)


async def main():
    monitor = ChannelMonitor()
    await monitor.initialize()

    try:
        # Получаем входной канал и пользователя
        input_channel = await monitor.client.get_entity(config.CHANNEL_ID)
        user = await monitor.client.get_entity(config.USER_ID)

        print(f"\n🔍 Мониторинг канала: {input_channel.title}")
        print(f"👤 Пользователь: {user.first_name if user.first_name else user.username}\n")

        # Запускаем фоновые задачи
        tasks = [
            asyncio.create_task(monitor.monitor_output_channel()),
            asyncio.create_task(monitor.check_timeouts()),
            asyncio.create_task(monitor.process_message_queue())
        ]

        # Основной цикл обработки сообщений
        while True:
            try:
                history = await monitor.client(GetHistoryRequest(
                    peer=input_channel,
                    limit=100,
                    offset_id=0,
                    min_id=monitor.last_checked_id
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
        await monitor.client.disconnect()
        print("🔴 Бот остановлен")


if __name__ == '__main__':
    print("🟢 Запуск бота для обработки артикулов")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🔴 Бот остановлен пользователем")