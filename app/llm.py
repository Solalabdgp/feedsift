"""Слой LLM. Синхронный, вызывается из Celery-воркера.

Две роли в одном вызове там, где это возможно:
  A — классификатор серой зоны / записей с болью без явного найма;
  B — RU-саммари для карточки уведомления.

Ограничение квоты — трёхслойное:
  1. Celery rate_limit на очереди (задаётся в celery_app);
  2. Redis-счётчик rate:groq:{circuit}:min;
  3. Redis-счётчик rate:groq:{circuit}:day.
При исчерпании — НЕ ретрай, а немедленный отказ: вызывающий помечает запись
decided_by="rules_only". Ретрай-цикл сжёг бы дневной бюджет за минуту.

Провайдер отдаёт остатки лимитов в заголовках ответа (x-ratelimit-*) — они читаются
на каждом ответе и пишутся в лог, чтобы видеть фактический остаток, а не догадки.
Связывающее ограничение на практике — ТОКЕНЫ, а не количество запросов.
"""
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import structlog

from app.config import settings
from app.redis_client import KEY_GROQ_POOL_BLOCKED, KEY_RATE_LLM_DAY, KEY_RATE_LLM_MIN

log = structlog.get_logger("llm")

API_BASE = "https://api.groq.com/openai/v1"
MAX_INPUT_CHARS = 6000

# Суточное окно счётчика. У Groq точная зона сброса из API не читается, поэтому
# берём UTC-полночь — это осознанная аппроксимация, а не подтверждённый факт.
# Настоящий предохранитель — fail-closed на реальном 429, а не это число.
QUOTA_RESET_TZ = "UTC"

SYSTEM_BRIEF = (
    "Ты бинарный классификатор записей из ленты. Задача — решить, описывает ли автор "
    "СВОЮ конкретную задачу или проблему, по которой ему можно предложить работу, "
    "или это запись другого рода.\n\n"
    "Профиль работ, под который идёт отбор: заказная разработка и автоматизация — "
    "сайты и веб-приложения, боты и мини-приложения, AI-ассистенты и чат-боты по базе "
    "знаний, парсеры, интеграции API, автоматизация процессов, внутренние инструменты "
    "и системы учёта. Конкретный язык и платформа значения не имеют: если это "
    "программирование под заказ, оно подходит.\n\n"
    "Цена ошибок разная. Ложное срабатывание — потраченное впустую внимание. "
    "Пропущенная запись — упущенная возможность. Баланс на сегодня: объём важнее "
    "идеальной чистоты. Если запись правдоподобно тянет на разговор о работе — "
    "пропускай. Отсекай уверенно только то, что заказом не является ни при каком "
    "прочтении: чужие советы, самопиар, теоретические споры, поиск работы."
)

SCHEMA_HINT = """Ставь is_lead=true ТОЛЬКО если выполнены ВСЕ ЧЕТЫРЕ условия.

1. КОНКРЕТНАЯ ЗАДАЧА. Описана конкретная работа или конкретная проблема самого автора:
   его сайт, его бот, его данные, его процесс. Не абстрактный вопрос «а как вы делаете X»,
   не тема для обсуждения, не размышление.

2. АВТОР ОПИСЫВАЕТ СВОЮ ПРОБЛЕМУ. Явное намерение нанять исполнителя НЕ ТРЕБУЕТСЯ.
   Достаточно, что он говорит о СВОЁМ процессе, активе или задаче, с которыми у него
   есть затруднение: не работает, ломается, отнимает время, не получается сделать.
   Ему не обязательно осознавать, что это можно заказать, и не обязательно просить
   помощи — жалоба или простая констатация факта тоже считаются. Он НЕ соискатель,
   НЕ коллега-разработчик, делящийся опытом или советом, НЕ подрядчик со своим
   оффером, и не тот, кто сравнивает инструменты ради самого сравнения, без привязки
   к своей текущей проблеме.

3. В ПРОФИЛЕ. Задача — заказная разработка или автоматизация. Список примеров выше не
   исчерпывающий: незнакомая платформа, фреймворк или язык сами по себе НЕ повод
   ставить out_of_scope. Плагин под редактор, расширение для браузера, скрипт под
   чужую CRM — всё это в профиле.
   Вне профиля только то, где программирования нет вовсе: отрисовка макетов и логотипов,
   написание текстов, перевод, репетиторство, ручное форматирование документов,
   маркетинговая консультация, работа руками.

4. МОЖНО НАЗВАТЬ ЦЕНУ. По записи реально написать ответ вида «могу сделать X, стоит
   примерно Y», и это не выглядело бы нелепо.

Не выполнено хотя бы одно — is_lead=false.

ОСОБЫЙ СЛУЧАЙ: операционная боль владельца бизнеса. Такой человек обычно НЕ ищет
разработчика и даже не думает, что его задачу можно заказать. Он жалуется на рутину и
спрашивает «а вы чем пользуетесь?». Это ВСЁ РАВНО is_lead=true, если в записи описан
ЕГО СОБСТВЕННЫЙ конкретный рабочий процесс, который ведётся вручную и который заменяется
софтом: ведёт клиентов, заказы, записи или склад в таблице; теряет данные; дублирует
ввод; тратит часы на выгрузки и сверки.

ВТОРОЙ ОСОБЫЙ СЛУЧАЙ: у автора что-то СВОЁ сломалось или не получается, и он спрашивает
сообщество. Сайт, магазин, бот, интеграция, плагин — его собственный актив работает не так,
он сам починить не может и пишет за помощью. Это is_lead=true, а НЕ discussion.
То, что он обратился к сообществу, а не к подрядчику, ничего не меняет: он просто не думает,
что это можно заказать. Сюда же — «мой магазин на X, мне нужно прикрутить Y, какие варианты?».
Отличие от настоящей discussion: там нет своего сломанного актива, там теория, спор
об архитектуре, опрос мнений или чужой опыт.

УТОЧНЕНИЕ (по факту ручного аудита размеченной выборки): устойчиво находится паттерн
ложных отклонений — реальная операционная боль классифицируется как discussion/survey
только потому, что автор не формулирует запрос как явный найм, а как «кто как решает»,
«что посоветуете», «есть ли у кого такой опыт с X».

Явная формулировка «ищу исполнителя»/«найму» НЕ ОБЯЗАТЕЛЬНА для is_lead=true. Если
автор описывает СВОЙ конкретный операционный процесс/актив (свой магазин, свою фирму,
свою CRM) и явно испытывает с ним затруднение — даже в форме вопроса «кто как решает»,
«что посоветуете», «есть ли у кого такой опыт с X» — это всё ещё is_lead=true, если
проблема по существу решается софтом/автоматизацией/консультацией по теме профиля.

Разница с discussion: в discussion человек не описывает СВОЙ актив с конкретной болью,
а рассуждает абстрактно, делится готовым решением, или разбирает чужой кейс/статью.

Разница с survey: в survey человек собирает чужой опыт для СВОЕГО будущего продукта/
исследования, а не решает свою текущую операционную проблему — если автор одновременно
и спрашивает чужой опыт, И описывает свою нерешённую боль — это is_lead=true, не survey.

Условие — конкретика СВОЕГО процесса: что именно ведётся, какой объём, что ломается.
Общий вопрос без описанного своего процесса («какой CRM лучше?» без единой детали) —
по-прежнему discussion. И если автор твёрдо намерен собрать всё сам и спрашивает только
про инструмент для самостоятельной сборки, либо он сам разработчик — не лид.

Форма изложения роли не играет. Не обязателен даже вопрос. Жалоба, ворчание, простое
описание сломанного/неудобного процесса без единого знака вопроса — это ТОЖЕ
is_lead=true, если выполняется условие 1: описана КОНКРЕТНАЯ СВОЯ проблема, а не
абстракция. «Ищет ли автор исполнителя» вообще не часть критерия. Значение имеет
только одно: своя ли это проблема и достаточно ли она конкретна, чтобы по ней можно
было предложить работу (условия 1 и 4 выше).

Это НЕ означает, что discussion/survey/tool_comparison исчезают — они по-прежнему
существуют, но планка для попадания в них ПОДНИМАЕТСЯ (сужается определение), а не
опускается. Ниже — уточнённые, более узкие описания.

Чаще всего is_lead=false выглядит так (проставь класс в reject_reason):
- "discussion" — ТОЛЬКО когда своего конкретного случая нет вообще: чистая теория,
  спор о подходах/архитектуре, обсуждение чужого кейса или статьи, размышление вслух
  без привязки к своему активу или процессу. Если есть хоть какой-то свой сломанный
  процесс — это уже не discussion, даже без единого вопроса в записи.
- "survey" — ТОЛЬКО когда автор собирает чужой опыт для своего будущего продукта или
  исследования И при этом не описывает никакой текущей своей нерешённой боли.
- "tool_comparison" — ТОЛЬКО сравнение инструментов само по себе, без привязки к
  своей текущей проблеме («какой из этих пяти инструментов лучше в целом»). Если
  сравнение идёт в контексте «у меня сломано X, на что заменить» — это уже не
  tool_comparison, это лид с описанной болью.
- "advice_share" — автор сам делится советом, наблюдением или выводом и ничего не просит
- "builder_diary" — дневник билдера, отчёт о прогрессе, «день 1 из 495»
- "self_promo" — реклама своего продукта, запуск, просьба о фидбеке
- "job_seeker" — автор сам ищет работу или спрашивает, стоит ли ему туда устраиваться
- "service_offer" — автор предлагает свои услуги
- "out_of_scope" — задача есть, но не про разработку софта
- "hiring_employee" — ищут штатного сотрудника, релокацию, фултайм в компанию
- "already_solved" — вопрос в записи уже закрыт
- "" — оставь пустым, если is_lead=true

ВАЖНО: плотность технических слов (automation, workflow, AI, SaaS, no-code) НИЧЕГО не значит.
В тематических источниках ими насыщена любая болтовня. Смотри только на то, просят ли
сделать конкретную работу.

Верни СТРОГО один JSON-объект без markdown-обёртки:
{
  "is_lead": true|false,
  "confidence": число от 0 до 1,
  "reject_reason": "класс из списка выше, либо пустая строка",
  "lead_type": "hiring_intent" | "pain_point" | "not_a_lead",
  "summary_ru": "2-3 предложения по-русски: что за запись и что человеку нужно",
  "need_ru": "одной строкой по-русски: конкретная работа, которую можно предложить (или пустая строка)",
  "budget_hint": "бюджет/ставка дословно как в записи, или пустая строка",
  "red_flags_ru": ["короткие строки по-русски: оплата долей вместо денег, нереальный бюджет, посредник, уже закрыто"]
}

Заполнение:
- summary_ru — всегда, даже при is_lead=false.
- need_ru — только при is_lead=true, иначе пустая строка. Не выдумывай конкретику,
  которой нет в записи.
- budget_hint — только если бюджет реально упомянут. Не выдумывай.
- confidence — насколько ты уверен В СВОЁМ ВЕРДИКТЕ, а не в том, что это лид.
- lead_type="hiring_intent" при явном найме, "pain_point" когда автор описал свою боль,
  но исполнителя прямо не просит, "not_a_lead" при is_lead=false."""


# ============================================================================
# BROWSE-КОНТУР: второй, независимый мониторинг по смежным нишам. Формат
# JSON-ответа — тот же, что у SCHEMA_HINT выше (is_lead/confidence/summary_ru/...),
# но критерий is_lead НАМНОГО шире: "любая боль/проблема, хотя бы отдалённо
# связанная с темой", а не явное намерение нанять исполнителя. Это pull-поток
# для ручного просмотра через /browse в боте (app/bot.py), не автоматизированные
# лид-уведомления — цена ложного срабатывания здесь низкая (карточка просто
# пролистывается), поэтому объём важнее точности ещё сильнее, чем в основном
# пайплайне.
# ============================================================================

SYSTEM_BRIEF_BROWSE = (
    "Ты помогаешь находить темы для разговора в смежных нишах: управление арендой "
    "и недвижимостью, бухгалтерия и налоги, малый бизнес и консалтинг, CRM и учёт, "
    "no-code и автоматизация. Профиль работ: сайты и веб-приложения, разработка на "
    "заказ, боты и мини-приложения, AI-ассистенты, автоматизация процессов, CRM и "
    "системы учёта под задачу.\n\n"
    "Это НЕ строгий поиск явного найма — это широкий просмотр на предмет любой боли "
    "или проблемы, которая хотя бы отдалённо связана с профилем. Решение, стоит ли "
    "откликаться и как, принимает человек. Твоя задача — не отсеивать всё, что не "
    "выглядит прямым заказом, а поднимать наверх всё, что может быть полезно увидеть. "
    "Ложное срабатывание здесь дёшево: карточка просто пролистывается. Пропущенный "
    "сигнал дороже: тема, ради которой контур и существует, тихо проходит мимо."
)

SCHEMA_HINT_BROWSE = """Ставь is_lead=true, если запись описывает ЛЮБУЮ конкретную боль или
проблему автора (не абстрактную дискуссию), которая хотя бы отдалённо связана с одной из тем:
сайты и веб-приложения, разработка на заказ, боты и мини-приложения, AI-ассистенты,
автоматизация процессов и интеграции, CRM и учёт клиентов/лидов/заказов, ведение
бухгалтерии и налогов, управление арендой и объектами недвижимости, операционка малого
бизнеса и консалтинга. Явного намерения нанять исполнителя НЕ требуется — достаточно, что
проблема в принципе решается софтом или автоматизацией, и автор описал СВОЙ конкретный
случай, а не просит общего мнения.

Критерий здесь ощутимо мягче, чем "явный заказ": запись о том, что кто-то вручную сверяет
таблицы, теряет учёт платежей, тонет в бумажной бухгалтерии, вручную заносит лиды в CRM,
путается в формулах таблицы для учёта — всё это is_lead=true, даже если автор не думает
о найме исполнителя вообще и никого не просит о помощи напрямую.

is_lead=false только если в записи НЕТ никакой конкретной боли/проблемы автора: чистая
теория, общий опрос без деталей, самопиар, вопрос-справочник без связи с профилем
("what's the going rate for X"), обсуждение регуляторики/права без операционной боли.

УТОЧНЕНИЕ (по факту ручного аудита размеченной выборки): устойчиво находится
паттерн ложных отклонений — реальная операционная боль классифицируется как
discussion/survey только потому, что автор не формулирует запрос как явный найм,
а как «кто как решает», «что посоветуете», «есть ли у кого такой опыт с X».

Явная формулировка «ищу исполнителя»/«найму» НЕ ОБЯЗАТЕЛЬНА для is_lead=true. Если
автор описывает СВОЙ конкретный операционный процесс/актив (свой магазин, свою фирму,
своё производство, свою CRM) и явно испытывает с ним затруднение — даже в форме вопроса
«кто как решает», «что посоветуете», «есть ли у кого такой опыт с X» — это всё ещё
is_lead=true, если проблема по существу решается софтом/автоматизацией/консультацией
по теме профиля.

Разница с discussion: в discussion человек не описывает СВОЙ актив с конкретной болью,
а рассуждает абстрактно, делится готовым решением, или разбирает чужой кейс/статью.

Разница с survey: в survey человек собирает чужой опыт для СВОЕГО будущего продукта/
исследования, а не решает свою текущую операционную проблему — если автор одновременно
и спрашивает чужой опыт, И описывает свою нерешённую боль — это is_lead=true, не survey.

Классы для reject_reason при is_lead=false:
- "discussion" — общая дискуссия или теоретический вопрос без своего конкретного случая
- "survey" — опрос, AMA, "поделитесь опытом"
- "self_promo" — реклама своего продукта/услуги
- "off_topic" — тема есть, но абсолютно не связана ни с одной темой выше (например, чистый
  вопрос права/регуляторики без операционной боли)
- "already_solved" — вопрос в записи уже закрыт
- "" — оставь пустым, если is_lead=true

Верни СТРОГО один JSON-объект без markdown-обёртки:
{
  "is_lead": true|false,
  "confidence": число от 0 до 1,
  "reject_reason": "класс из списка выше, либо пустая строка",
  "lead_type": "hiring_intent" | "pain_point" | "not_a_lead",
  "summary_ru": "2-3 предложения по-русски: что за запись и в чём боль",
  "need_ru": "одной строкой по-русски: в чём может быть полезна автоматизация/софт (или пустая строка)",
  "budget_hint": "бюджет/ставка дословно как в записи, или пустая строка",
  "red_flags_ru": []
}

Заполнение:
- summary_ru — всегда, даже при is_lead=false.
- need_ru — только при is_lead=true, иначе пустая строка. Не выдумывай конкретику,
  которой нет в записи.
- budget_hint — только если бюджет реально упомянут. Не выдумывай.
- confidence — насколько ты уверен В СВОЁМ ВЕРДИКТЕ, а не в том, что это лид.
- lead_type="pain_point" почти всегда (это широкий, не строгий контур), "hiring_intent"
  только если найм явно упомянут, "not_a_lead" при is_lead=false.
- red_flags_ru — оставляй пустым массивом всегда: это поток для просмотра, разбор
  рисков здесь не нужен."""


class QuotaExhausted(Exception):
    pass


def _seconds_to_quota_reset() -> int:
    tz = ZoneInfo(QUOTA_RESET_TZ)
    now = datetime.now(tz)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((tomorrow - now).total_seconds()))


def quota_acquire(redis, circuit: str = "main") -> bool:
    """Fail-closed: True только если оба счётчика внутри лимита. Инкремент атомарный.

    Минутный лимит проверяется ПЕРВЫМ, дневной — вторым. Порядок важен: при обратном
    отказ по минутному лимиту всё равно списывал бы единицу дневной квоты, и частые
    минутные отказы съедали бы дневной бюджет впустую. Ошибка была консервативной
    (лишний отказ, не лишний вызов), но бессмысленной.

    circuit="main" по умолчанию — поведение контура A не меняется. Для browse-контура
    (отдельный Groq-ключ/аккаунт, добавлено 30.08.2026) ключи в Redis свои
    (KEY_RATE_LLM_MIN/DAY шаблонизированы по circuit, см. app/redis_client.py) —
    расход browse не списывается со счётчика main и наоборот.
    """
    key_min = KEY_RATE_LLM_MIN.format(circuit=circuit)
    key_day = KEY_RATE_LLM_DAY.format(circuit=circuit)

    minute = redis.incr(key_min)
    if minute == 1:
        redis.expire(key_min, 60)
    if minute > settings.groq_rpm_limit:
        log.warning("llm_minute_quota_exhausted", circuit=circuit, used=minute, limit=settings.groq_rpm_limit)
        return False

    day = redis.incr(key_day)
    if day == 1:
        redis.expire(key_day, _seconds_to_quota_reset())
    if day > settings.groq_rpd_limit:
        log.warning("llm_daily_quota_exhausted", circuit=circuit, used=day, limit=settings.groq_rpd_limit)
        return False

    return True


def quota_state(redis, circuit: str = "main") -> dict:
    return {
        "day_used": int(redis.get(KEY_RATE_LLM_DAY.format(circuit=circuit)) or 0),
        "day_limit": settings.groq_rpd_limit,
        "min_used": int(redis.get(KEY_RATE_LLM_MIN.format(circuit=circuit)) or 0),
        "min_limit": settings.groq_rpm_limit,
    }


def _build_user_message(
    title: str,
    body: str,
    source: str,
    mode: str,
    rule_hint: str,
    schema_hint: str = SCHEMA_HINT,
) -> str:
    """SYSTEM_BRIEF уходит в system-сообщение, остальное — в user-сообщение.

    schema_hint — параметр (не глобал), чтобы browse-контур мог подставить
    SCHEMA_HINT_BROWSE без ветвления внутри этой функции (см. call_llm(prompt_variant=)).
    Дефолт — строгий SCHEMA_HINT, поведение основного контура не меняется.
    """
    item = f"Источник: {source}\nЗаголовок: {title}\n\nТекст записи:\n{body}"[:MAX_INPUT_CHARS]
    if mode == "translate":
        task = (
            "Запись уже отобрана правилами как релевантная. Классификацию не пересматривай: "
            "поставь is_lead=true. Нужен только качественный русский пересказ."
        )
    else:
        task = (
            "Правила по ключевым словам не смогли решить однозначно. "
            f"Подсказка от правил: {rule_hint}. Реши сам и объясни коротко."
        )
    return f"{task}\n\n{schema_hint}\n\n---\n{item}\n---"


def _parse_response(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text)


_PROMPT_VARIANTS = {
    "strict": (SYSTEM_BRIEF, SCHEMA_HINT),
    "browse": (SYSTEM_BRIEF_BROWSE, SCHEMA_HINT_BROWSE),
}


def call_llm(
    title: str,
    body: str,
    source: str,
    mode: str = "classify_and_translate",
    rule_hint: str = "",
    prompt_variant: str = "strict",
    api_key: str | None = None,
) -> tuple[dict, dict]:
    """Возвращает (verdict, meta). Бросает исключение при ошибке сети/API — вызывающий решает.

    Ретраев здесь нет намеренно: 429 должен приводить к fail-closed на уровне
    celery-задачи (decided_by="rules_only"), а не к циклу добивания квоты. Ротация
    ключей (несколько попыток С РАЗНЫМИ ключами на 429) — на уровень выше, в
    call_llm_with_pool ниже; сама эта функция всегда делает ровно один HTTP-запрос.

    prompt_variant="strict" (дефолт) — SYSTEM_BRIEF/SCHEMA_HINT, строгий критерий
    основного контура. "browse" — SYSTEM_BRIEF_BROWSE/SCHEMA_HINT_BROWSE, широкий
    критерий второго контура.

    api_key — явный оверрайд ключа (для ротации по пулу). None (дефолт) —
    используется settings.groq_api_key ЭТОГО процесса.
    """
    key = api_key or settings.groq_api_key
    if not key:
        raise RuntimeError("GROQ_API_KEY не задан")

    system_brief, schema_hint = _PROMPT_VARIANTS.get(prompt_variant, _PROMPT_VARIANTS["strict"])

    payload = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": system_brief},
            {
                "role": "user",
                "content": _build_user_message(title, body, source, mode, rule_hint, schema_hint),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    started = time.monotonic()
    resp = httpx.post(
        f"{API_BASE}/chat/completions",
        # Ключ уходит в заголовке, а не в query-строке. httpx на уровне INFO печатает
        # только метод и URL, заголовки — никогда; вдобавок httpx/httpcore переведены
        # на WARNING в app/logging_setup.py. То есть в логах ключа нет ни в каком виде.
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=settings.groq_timeout_sec,
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    # Реальные лимиты провайдер отдаёт в заголовках. Пишем в лог, чтобы видеть
    # фактический остаток, а не догадки.
    limits = {
        "rl_req_left": resp.headers.get("x-ratelimit-remaining-requests"),
        "rl_req_limit": resp.headers.get("x-ratelimit-limit-requests"),
        "rl_tok_left": resp.headers.get("x-ratelimit-remaining-tokens"),
        "rl_reset_req": resp.headers.get("x-ratelimit-reset-requests"),
    }
    if resp.status_code == 429:
        log.warning("llm_rate_limited_by_provider", retry_after=resp.headers.get("retry-after"), **limits)

    # Тело ответа НЕ логируем: там пересказ чужой записи + возможные персональные
    # данные. В лог идут только метаданные.
    resp.raise_for_status()
    data = resp.json()
    usage = data.get("usage", {})
    message = data["choices"][0]["message"]
    content = message.get("content") or ""
    if not content.strip():
        raise ValueError("LLM вернул пустой content")
    verdict = _parse_response(content)

    details = usage.get("completion_tokens_details") or {}
    meta = {
        "latency_ms": latency_ms,
        "prompt_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "model": settings.groq_model,
    }
    log.info("llm_call_ok", mode=mode, prompt_variant=prompt_variant, **meta, **limits)
    return verdict, meta


# ============================================================================
# ПУЛ ЗАПАСНЫХ КЛЮЧЕЙ (main-контур)
#
# На реальном объёме main-контур упирается в дневной токен-лимит ключа раньше конца
# суток — fail-closed уходит в rules_only (поведение celery_app.py). Пул запасных
# ключей (settings.groq_key_pool в app/config.py) снимает это: при 429 от текущего
# ключа — не сразу rules_only, а попытка следующего доступного ключа из пула
# ДЛЯ ТОГО ЖЕ вызова; штатный fail-closed — только если упёрлись все.
#
# browse-контур пул НЕ использует: call_llm_with_pool с circuit!="main" — прямой
# проброс в call_llm() без ротации, с ключом settings.groq_api_key ЭТОГО процесса.
# ============================================================================

# TTL для 429 без явной классификации или с нераспознанным текстом ошибки —
# осторожный дефолт, ближе к TPM/RPM (короткий), чем к TPD (сутки): не хотим
# по ошибке блокировать рабочий ключ на сутки из-за незнакомой формулировки.
_RATE_LIMIT_UNKNOWN_TTL_SEC = 120

# TPM/RPM — минутные окна. retry_after от Groq в логах видели правдоподобным
# (231с), но подстраховываемся границами: не короче 30с (Groq иногда занижает),
# не длиннее 15 минут (не хотим блокировать ключ на TPD-подобный срок за
# минутный лимит, который сам по себе сбросится быстро).
_TPM_RPM_MIN_TTL_SEC = 30
_TPM_RPM_MAX_TTL_SEC = 900
_TPM_RPM_DEFAULT_TTL_SEC = 60


def _mask_key(key: str) -> str:
    """Для логов — никогда не печатать ключ целиком."""
    return f"...{key[-4:]}" if len(key) >= 4 else "***"


def _classify_rate_limit(message: str) -> str:
    """Грубая классификация текста ошибки 429 от Groq. 'tpd'|'tpm_rpm'|'unknown'.

    Судим по тексту message в теле ответа — точной документированной схемы кодов
    у Groq на 429 нет, только человекочитаемая фраза вида "...on tokens per day (TPD)".
    """
    upper = message.upper()
    if "TPD" in upper or "TOKENS PER DAY" in upper:
        return "tpd"
    if "TPM" in upper or "RPM" in upper or "PER MINUTE" in upper:
        return "tpm_rpm"
    return "unknown"


def _pool_blocked_key(circuit: str, index: int) -> str:
    return KEY_GROQ_POOL_BLOCKED.format(circuit=circuit, index=index)


def is_pool_key_blocked(redis, circuit: str, index: int) -> bool:
    return bool(redis.exists(_pool_blocked_key(circuit, index)))


def _block_pool_key(redis, circuit: str, index: int, key: str, exc: httpx.HTTPStatusError) -> None:
    """Помечает ключ с данным index недоступным на TTL, зависящий от класса 429.

    TPD — блокируем до сброса дневной квоты у Groq. Он не сообщает зону сброса точно
    (та же оговорка, что у _seconds_to_quota_reset выше — UTC-полночь, аппроксимация,
    не подтверждённый факт), но переоценить TTL здесь безопаснее, чем недооценить:
    лишний час блокировки рабочего ключа дешевле, чем повторные 429 на исчерпанный.
    TPM/RPM — короткий TTL по retry-after из ответа, с разумными границами.
    """
    resp = exc.response
    message = ""
    try:
        body = resp.json()
        message = str((body.get("error") or {}).get("message") or "") or resp.text
    except Exception:
        message = resp.text or ""
    kind = _classify_rate_limit(message)

    if kind == "tpd":
        ttl = _seconds_to_quota_reset()
    else:
        retry_after_raw = resp.headers.get("retry-after")
        try:
            retry_after = int(float(retry_after_raw)) if retry_after_raw else _TPM_RPM_DEFAULT_TTL_SEC
        except ValueError:
            retry_after = _TPM_RPM_DEFAULT_TTL_SEC
        if kind == "unknown":
            retry_after = max(retry_after, _RATE_LIMIT_UNKNOWN_TTL_SEC)
        ttl = max(_TPM_RPM_MIN_TTL_SEC, min(retry_after, _TPM_RPM_MAX_TTL_SEC))

    redis.set(_pool_blocked_key(circuit, index), "1", ex=ttl)
    log.warning(
        "groq_pool_key_blocked",
        circuit=circuit,
        key_index=index,
        key_suffix=_mask_key(key),
        rate_limit_kind=kind,
        ttl_sec=ttl,
    )


def call_llm_with_pool(
    title: str,
    body: str,
    source: str,
    redis,
    circuit: str = "main",
    mode: str = "classify_and_translate",
    rule_hint: str = "",
    prompt_variant: str = "strict",
) -> tuple[dict, dict]:
    """Обёртка над call_llm с ротацией ключей из пула. ТОЛЬКО для circuit="main".

    circuit!="main" (сейчас — только "browse") — прямой проброс в call_llm с ключом
    settings.groq_api_key ЭТОГО процесса, пул не используется вообще.

    redis — синхронный клиент (get_sync_redis()), эта функция вызывается из
    Celery-задачи, не из asyncio-кода — тот же клиент, что использует quota_acquire.

    При 429 от текущего ключа — блокирует его в Redis (см. _block_pool_key) и
    пробует следующий НЕ заблокированный ключ из пула для ЭТОГО ЖЕ вызова.
    Если пул исчерпан (все ключи заблокированы) — пробрасывает исключение дальше:
    celery_app.py ловит его как обычно и переводит матч в decided_by="rules_only"
    (штатный fail-closed, не меняется).
    """
    if circuit != "main":
        return call_llm(
            title, body, source, mode=mode, rule_hint=rule_hint, prompt_variant=prompt_variant
        )

    pool = settings.groq_key_pool
    if not pool:
        # Пул пуст (переменные не заданы) — один ключ из settings.groq_api_key
        # внутри call_llm, никакой ротации.
        return call_llm(
            title, body, source, mode=mode, rule_hint=rule_hint, prompt_variant=prompt_variant
        )

    last_exc: Exception | None = None
    for index, key in enumerate(pool):
        if is_pool_key_blocked(redis, circuit, index):
            continue
        try:
            verdict, meta = call_llm(
                title, body, source, mode=mode, rule_hint=rule_hint,
                prompt_variant=prompt_variant, api_key=key,
            )
            meta["groq_key_index"] = index
            return verdict, meta
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429:
                raise
            _block_pool_key(redis, circuit, index, key, exc)
            log.warning(
                "groq_key_rotated",
                circuit=circuit,
                blocked_key_index=index,
                blocked_key_suffix=_mask_key(key),
                pool_size=len(pool),
            )
            last_exc = exc
            continue

    log.error("groq_pool_exhausted", circuit=circuit, pool_size=len(pool))
    raise last_exc or RuntimeError("Groq key pool exhausted with no captured exception")
