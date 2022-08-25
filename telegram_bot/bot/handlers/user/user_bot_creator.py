from subprocess import Popen
from datetime import timedelta

from loguru import logger
from aiogram import Dispatcher, Bot
from aiogram.types import Message
from aiogram.dispatcher import FSMContext
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, FloodWait, PhoneNumberInvalid, PhoneCodeExpired, \
    ApiIdInvalid, PasswordHashInvalid

from telegram_bot.bot.database.methods import create_user_bot_session, get_user_by_id_telegram_id, create_user, \
    check_vip
from telegram_bot.bot.keyboards import me_telegram_keyboard
from telegram_bot.bot.keyboards import main_keyboard_pro_bot_started, main_keyboard_trial_bot_started
from telegram_bot.bot.misc import CreateUserBotState, start_user_bot

__sessions = {}
__process: dict[int, Popen] = {}


async def __start_input_user_settings(msg: Message, state: FSMContext) -> None:
    bot: Bot = msg.bot
    user_id = msg.from_user.id
    user = get_user_by_id_telegram_id(user_id)
    if __process.get(msg.from_user.id, None):
        if check_vip(user_id):
            await bot.send_message(user_id, "Ваш бот уже запущен!", reply_markup=main_keyboard_pro_bot_started)
        else:
            await bot.send_message(user_id, "Ваш бот уже запущен!", reply_markup=main_keyboard_trial_bot_started)
        return
    if user:
        if user.session:
            start_user_bot(user.session.session, msg.from_user.id, user.vip)
            await state.finish()
            await bot.send_message(msg.from_user.id, "Ваш прошлый сеанс ещё активен!")
            return
    else:
        create_user(user_id)
    await bot.send_message(user_id, 'Узнать данные можно 👇', reply_markup=me_telegram_keyboard)
    await bot.send_message(user_id, "Введите ваш api-id:")
    await state.set_state(CreateUserBotState.API_ID)


async def __stop_bot(msg: Message):
    bot: Bot = msg.bot
    bot_process = __process[msg.from_user.id]
    bot_process.kill()
    await bot.send_message(msg.from_user.id, "User bot остановлен!")


async def __input_api_id(msg: Message, state: FSMContext) -> None:
    bot: Bot = msg.bot
    if msg.text.isdigit():
        async with state.proxy() as data:
            data['write_api_id'] = int(msg.text)
    else:
        await bot.send_message(msg.from_user.id, "api-id должен состоять только из цифр! Вы где-то ошиблись!")
        return
    await bot.send_message(msg.from_user.id, "Введите api-hash:")
    await state.set_state(CreateUserBotState.API_HASH)


async def __input_api_hash(msg: Message, state: FSMContext) -> None:
    bot: Bot = msg.bot
    if len(msg.text) != 32:
        await bot.send_message(msg.from_user.id, "api-hash должен состоять из 32 символов! Вы где-то ошиблись!")
        return

    async with state.proxy() as data:
        data['write_api_hash'] = msg.text
    await bot.send_message(msg.from_user.id, "Введите ваш номер телефона:")
    await state.set_state(CreateUserBotState.PHONE)


async def __input_phone(msg: Message, state: FSMContext) -> None:
    bot: Bot = msg.bot
    user_data = await state.get_data()

    try:
        client = Client(
            name=str(msg.from_user.id),
            api_id=user_data["write_api_id"],
            api_hash=user_data["write_api_hash"],
            password=None,
            in_memory=True
        )
        await client.connect()
    except (ApiIdInvalid, PasswordHashInvalid) as e:
        logger.error(e)
        await bot.send_message(msg.from_user.id, "Введены не правильные ключи доступа. Начните все заново")
        await state.finish()
        return
    try:
        code = await client.send_code(msg.text)
    except PhoneNumberInvalid as e:
        logger.error(e)
        await bot.send_message(msg.from_user.id, "Введен не правильный номер телефона.\nПовторите попытку!")
        return
    except FloodWait as e:
        logger.error(e)
        await bot.send_message(msg.from_user.id, f"Не удалось отправить смс!\n"
                                                 f"Повторите попытку через - {timedelta(seconds=e.value)}")
        await state.finish()
        return

    async with state.proxy() as data:
        data['write_phone'] = msg.text
        data['code'] = code
        __sessions[msg.from_user.id] = client

    await bot.send_message(msg.from_user.id, "Введите код подтверждения из телеграмма в таком формате 0-0-0-0-0: \n"
                                             "Важно ставить тире после каждой цифры!")
    await state.set_state(CreateUserBotState.AUTH_CODE)


async def __input_oauth_code(msg: Message, state: FSMContext) -> None:
    bot: Bot = msg.bot
    user_data = await state.get_data()
    code = "-".join(msg.text.split())

    client: Client = __sessions[msg.from_user.id]
    async with state.proxy() as data:
        data["auth_code"] = code

    try:
        await client.sign_in(
            user_data['write_phone'],
            user_data['code'].phone_code_hash,
            code,
        )
    except PhoneCodeInvalid as e:
        logger.error(e)
        await bot.send_message(msg.from_user.id, "Неверный код!\nПовторите авторизацию заново")
        return
    except PhoneCodeExpired as e:
        logger.error(e)
        await bot.send_message(msg.from_user.id, "Код подтверждения иссек, попробуйте заново")
        await state.finish()
        return
    except SessionPasswordNeeded as e:
        logger.error(e)
        await bot.send_message(msg.from_user.id, "Введи 2fa")
        await state.set_state(CreateUserBotState.TWO_FA_PASSWORD)
        return

    string_session = await client.export_session_string()

    if user := get_user_by_id_telegram_id(msg.from_user.id):
        create_user_bot_session(user, string_session)

    await client.disconnect()

    start_user_bot(string_session, msg.from_user.id, user.vip)
    __process[msg.from_user.id] = start_user_bot(string_session, msg.from_user.id, user.vip)
    del (__sessions[msg.from_user.id])
    if check_vip(msg.from_user.id):
        await bot.send_message(msg.from_user.id, "User bot запущен", reply_markup=main_keyboard_pro_bot_started)
    else:
        await bot.send_message(msg.from_user.id, "User bot запущен", reply_markup=main_keyboard_trial_bot_started)
    await state.finish()


async def __intput_2fa_password(msg: Message, state: FSMContext) -> None:
    bot: Bot = msg.bot
    client: Client = __sessions[msg.from_user.id]
    try:
        await client.check_password(password=msg.text)
    except PasswordHashInvalid as e:
        logger.error(e)
        await bot.send_message(msg.from_user.id, "Повтори")
        return

    string_session = await client.export_session_string()

    if user := get_user_by_id_telegram_id(msg.from_user.id):
        create_user_bot_session(user, string_session)

    await client.disconnect()

    __process[msg.from_user.id] = start_user_bot(string_session, msg.from_user.id, user.vip)
    del (__sessions[msg.from_user.id])
    if check_vip(msg.from_user.id):
        await bot.send_message(msg.from_user.id, "User bot запущен", reply_markup=main_keyboard_pro_bot_started)
    else:
        await bot.send_message(msg.from_user.id, "User bot запущен", reply_markup=main_keyboard_trial_bot_started)
    await state.finish()


async def __stop_register_user_bot(msg: Message, state: FSMContext) -> None:
    bot: Bot = msg.bot
    await state.finish()
    await bot.send_message(msg.from_user.id, "Авторизация юзер бота отменена!")


def _register_user_bot_handlers(dp: Dispatcher) -> None:
    dp.register_message_handler(__stop_register_user_bot, commands=['cancel'], state="*")
    dp.register_message_handler(__start_input_user_settings, content_types=['text'], text="Подключить бота", state=None)
    dp.register_message_handler(__stop_bot, content_types=['text'], text="Остановить бота", state=None)
    dp.register_message_handler(__input_api_id, content_types=['text'], state=CreateUserBotState.API_ID)
    dp.register_message_handler(__input_api_hash, content_types=['text'], state=CreateUserBotState.API_HASH)
    dp.register_message_handler(__input_phone, content_types=['text'], state=CreateUserBotState.PHONE)
    dp.register_message_handler(__input_oauth_code, content_types=['text'], state=CreateUserBotState.AUTH_CODE)
    dp.register_message_handler(__intput_2fa_password, content_types=['text'], state=CreateUserBotState.TWO_FA_PASSWORD)
