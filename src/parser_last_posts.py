import asyncio
import aiohttp
from telethon import TelegramClient
from telethon.tl.functions.messages import GetHistoryRequest
import config
import re
from datetime import datetime, timedelta


class ChannelMonitor:
    def __init__(self):
        self.last_checked_id = 0
        self.processed_ids = set()
        self.client = None
        self.output_channel = None
        self.message_store = {}
        self.article_timeouts = {}
        self.pending_responses = set()  # Для отслеживания ожидаемых ответов

    async def initialize(self):
        self.client = TelegramClient('new_user_session', config.API_ID, config.API_HASH)
        await self.client.start(config.PHONE_NUMBER)

        if isinstance(config.OUTPUT_CHANNEL_ID, str):
            self.output_channel = await self.client.get_entity(config.OUTPUT_CHANNEL_ID)
        else:
            self.output_channel = await self.client.get_entity(int(config.OUTPUT_CHANNEL_ID))

        print("✅ Клиент успешно инициализирован")

    async def send_to_protalk_api(self, message_text):
        url = f"https://api.pro-talk.ru/api/v1.0/ask/k7LZ7GN49P1UtvN1DJX4qYesMXml82Ij"
        headers = {'Content-Type': 'application/json'}
        payload = {
            "bot_id": 25815,
            "chat_id": "channel_monitor",
            "message": message_text
        }

        print(f"🔄 Отправка в API: {message_text[:50]}...")

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status == 200:
                        return await response.json()
                    print(f"⚠️ Ошибка API: {response.status}")
        except Exception as e:
            print(f"⚠️ Ошибка соединения с API: {str(e)}")
        return None

    def parse_second_bot_response(self, response_text):
        """Парсит ответ от второго бота"""
        result = {
            'name': None,
            'price': None,
            'stock_quantity': None,
            'found': False
        }

        try:
            if "Артикул не найден" in response_text:
                return result

            # Ищем наименование
            name_match = re.search(r'Наименование:\s*(.*)', response_text)
            if name_match:
                result['name'] = name_match.group(1).strip()

            # Ищем цену
            price_match = re.search(r'Цена за штуку:\s*([\d.,]+)', response_text)
            if price_match:
                result['price'] = float(price_match.group(1).replace(',', '.'))
                result['found'] = True

            # Ищем количество на складе
            stock_match = re.search(r'Количество на складе:\s*(\d+)', response_text)
            if stock_match:
                result['stock_quantity'] = int(stock_match.group(1))

        except Exception as e:
            print(f"⚠️ Ошибка парсинга ответа второго бота: {e}")

        return result

    async def process_api_response(self, user, api_response, original_message):
        if not api_response or 'done' not in api_response:
            return

        response_text = api_response['done'].strip()
        if not response_text or response_text in ('...', '…'):
            return

        # Разбиваем ответ на строки (каждая строка - отдельный артикул)
        lines = [line.strip() for line in response_text.split('\n') if line.strip()]

        articles_data = []

        for line in lines:
            if ':' not in line:
                continue

            try:
                article_part, quantity_part = line.split(':', 1)
                article = article_part.strip()
                quantity = quantity_part.strip().split()[0]
                quantity = float(quantity)

                articles_data.append({
                    'article': article,
                    'quantity': quantity,
                    'processed': False,
                    'found': None  # None - ожидается ответ, False - не найден, True - найден
                })

                # Отправляем каждый артикул отдельно во второй канал
                await self.client.send_message(self.output_channel, article)
                print(f"📤 Отправлен артикул во второй канал: {article}")

                # Запоминаем время отправки артикула
                self.article_timeouts[article] = datetime.now()
                self.pending_responses.add(article)

            except Exception as e:
                print(f"⚠️ Ошибка обработки строки '{line}': {e}")
                continue

        if articles_data:
            # Сохраняем все артикулы из этого сообщения
            self.message_store[original_message.id] = {
                'articles': articles_data,
                'original_message': original_message,
                'responses': [],  # Здесь будем хранить ответы по каждому артикулу
                'timestamp': datetime.now()
            }

    async def handle_output_channel_response(self, response_message):
        """Обрабатывает ответ из второго канала"""
        try:
            # Парсим ответ второго бота
            bot_data = self.parse_second_bot_response(response_message.message)

            # Ищем, к какому оригинальному сообщению относится этот ответ
            for msg_id, data in self.message_store.items():
                # Проверяем все артикулы в этом сообщении
                for article_data in data['articles']:
                    if (article_data['article'] in response_message.message and
                            not article_data['processed'] and
                            article_data['article'] in self.pending_responses):

                        # Помечаем артикул как обработанный
                        article_data['processed'] = True
                        article_data['found'] = bot_data['found']

                        # Сохраняем ответ по этому артикулу
                        if bot_data['found']:
                            data['responses'].append({
                                'article_data': article_data,
                                'bot_data': bot_data
                            })

                        # Удаляем из ожидаемых ответов
                        self.pending_responses.discard(article_data['article'])
                        if article_data['article'] in self.article_timeouts:
                            del self.article_timeouts[article_data['article']]

                        # Проверяем, получены ли все ответы
                        await self.check_complete_response(msg_id, data)
                        return

        except Exception as e:
            print(f"⚠️ Ошибка обработки ответа второго бота: {e}")

    async def check_complete_response(self, msg_id, data):
        """Проверяет, можно ли отправлять ответ"""
        # Проверяем, все ли артикулы обработаны (получили ответ или истек таймаут)
        all_processed = all(article['processed'] or
                            (article['article'] in self.article_timeouts and
                             (datetime.now() - self.article_timeouts[article['article']]) > timedelta(seconds=30))
                            for article in data['articles'])

        if all_processed:
            # Помечаем все артикулы с истекшим таймаутом как не найденные
            for article in data['articles']:
                if not article['processed'] and article['article'] in self.article_timeouts:
                    if (datetime.now() - self.article_timeouts[article['article']]) > timedelta(seconds=30):
                        article['processed'] = True
                        article['found'] = False
                        self.pending_responses.discard(article['article'])
                        del self.article_timeouts[article['article']]

            await self.send_combined_response(data)
            # Удаляем обработанное сообщение
            if msg_id in self.message_store:
                del self.message_store[msg_id]

    async def send_combined_response(self, message_data):
        """Формирует и отправляет объединенный ответ"""
        try:
            original_message = message_data['original_message']
            responses = message_data['responses']
            articles = message_data['articles']

            # Проверяем, есть ли хотя бы один расцененный артикул
            has_priced_articles = any(article.get('found', False) for article in articles)
            has_unpriced_articles = any(article.get('found', False) is False for article in articles)

            # Формируем ссылку на пост
            channel_entity = await self.client.get_entity(original_message.peer_id)
            post_link = f"https://t.me/c/{channel_entity.id}/{original_message.id}"

            # Формируем заголовок сообщения
            response_text = f"📎 [Ссылка на пост]({post_link})\n\n"

            total_sum = 0
            total_discount_sum = 0
            found_count = 0
            not_found_articles = []

            # Обрабатываем найденные артикулы
            if has_priced_articles:
                response_text += "✅ Расценены следующие артикулы:\n\n"

                for article in articles:
                    if article.get('found', False):
                        # Ищем ответ по этому артикулу
                        response = next((r for r in responses if r['article_data']['article'] == article['article']),
                                        None)
                        if response:
                            found_count += 1
                            article_data = response['article_data']
                            bot_data = response['bot_data']

                            # Рассчитываем стоимости
                            item_total = bot_data['price'] * article_data['quantity']
                            item_discount = item_total * 0.97

                            total_sum += item_total
                            total_discount_sum += item_discount

                            # Формируем текст с количеством на складе
                            stock_info = ""
                            if bot_data['stock_quantity'] is not None:
                                stock_info = f" ({bot_data['stock_quantity']} на складе)"

                            # Добавляем информацию об этом артикуле
                            response_text += (
                                f"🔹 Артикул: {article_data['article']}\n"
                                f"🏷️ Наименование: {bot_data['name'] or 'Не указано'}\n"
                                f"📦 Запрошенное количество: {article_data['quantity']}{stock_info}\n"
                                f"💰 Цена за единицу: {bot_data['price']:.2f}\n"
                                f"🧮 Итого по позиции: {item_total:.2f}\n"
                                f"🎁 Со скидкой 3%: {item_discount:.2f}\n\n"
                            )
                    elif article.get('found', False) is False:
                        not_found_articles.append(article['article'])

            # Добавляем информацию о нерасцененных артикулах
            if has_unpriced_articles:
                response_text += "\n🚫 Не расценены следующие артикулы:\n"
                for article in not_found_articles:
                    response_text += f"• {article}\n"
                response_text += "\n"

            # Добавляем итоговую сумму только если есть расцененные артикулы
            if has_priced_articles:
                response_text += (
                    f"💵 Общая сумма: {total_sum:.2f}\n"
                    f"💳 Общая сумма со скидкой: {total_discount_sum:.2f}\n"
                )

            # Отправляем сообщение пользователю только если есть что-то полезное
            if has_priced_articles or has_unpriced_articles:
                await self.client.send_message(
                    config.USER_ID,
                    response_text,
                    reply_to=original_message,
                    link_preview=False
                )
                print(f"✉️ Отправлен объединенный расчет для {len(articles)} артикулов")
            else:
                print("ℹ️ Нет информации для отправки (ни один артикул не обработан)")

        except Exception as e:
            print(f"⚠️ Ошибка формирования объединенного ответа: {e}")
    async def check_timeouts(self):
        """Проверяет таймауты ожидания ответов"""
        while True:
            try:
                now = datetime.now()
                # Проверяем все сообщения на завершенность
                for msg_id, data in list(self.message_store.items()):
                    await self.check_complete_response(msg_id, data)

                await asyncio.sleep(5)
            except Exception as e:
                print(f"⚠️ Ошибка проверки таймаутов: {e}")
                await asyncio.sleep(10)

    async def monitor_output_channel(self):
        """Мониторит ответы из второго канала"""
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
                        if msg.id > last_id and not msg.out:  # Только входящие сообщения
                            await self.handle_output_channel_response(msg)
                            last_id = msg.id

                await asyncio.sleep(5)
            except Exception as e:
                print(f"⚠️ Ошибка мониторинга второго канала: {e}")
                await asyncio.sleep(10)

    async def process_messages(self, user, messages):
        """Обрабатывает входящие сообщения"""
        new_messages = [msg for msg in messages if msg.id > self.last_checked_id]

        if not new_messages:
            return

        new_messages.sort(key=lambda x: x.id)
        messages_to_process = new_messages[-10:]

        for message in messages_to_process:
            if not message.message:
                continue

            print(f"📩 Обработка сообщения ID: {message.id}")

            api_response = await self.send_to_protalk_api(message.message)
            if api_response:
                await self.process_api_response(user, api_response, message)

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

        # Запускаем задачи мониторинга
        asyncio.create_task(monitor.monitor_output_channel())
        asyncio.create_task(monitor.check_timeouts())

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