"""Seed-словари для таблицы keywords — плейсхолдеры.

Боевые словари живут в БД и правятся на живую из бота (/keywords add ...);
этот модуль задаёт только ФОРМУ данных: какие типы словарей существуют, с какими
весами они сеются и как выглядит запись алиаса (term -> maps_to).

Термины хранятся строчными, без скобок: скобочные метки ([Hiring], [For Hire], [Task])
regex-альтернация с \\b не ловит — они обрабатываются отдельно через normalize.extract_tag.
"""

# --- Стек: технологии и платформы, попадающие в профиль исполнителя ---
# Ядро (вес 3): на этих терминах срабатывает мягкий сигнал "вопрос по теме".
STACK_CORE = [
    "python", "scraper", "web scraping", "automation", "api integration",
    "website", "landing page", "web app", "chatbot", "ai assistant",
    "workflow automation", "data extraction", "internal tool", "crm",
    "dashboard",
]

# Периферия (вес 1): фоновые, ambient-термины. Мягкий сигнал на них
# срабатывать НЕ должен — иначе в воронку льётся любая техническая болтовня.
STACK_PERIPH = [
    "webhook", "postgres", "sql", "docker", "rest api", "json", "csv",
    "integration", "server", "database", "cron", "analytics", "payment",
    "pdf", "vector database",
]

# --- Явный найм: автор прямо ищет исполнителя ---
# STRONG (вес 4) — формулировка не допускает другого прочтения.
HIRING_INTENT_STRONG = [
    "hiring a developer", "hire a developer", "need a developer",
    "looking for a developer", "contract developer needed",
    "need someone to build", "need someone to automate", "need someone to fix",
    "will pay", "paid gig", "freelance gig", "my budget",
]
# ВАЖНО: "for hire" сюда НЕ добавлять — это маркер противоположного смысла
# (автор сам продаёт услуги). Он лежит в ANTI.

# WEAK (вес 2) — запрос читается как найм, но может оказаться и вопросом.
HIRING_INTENT_WEAK = [
    "need a bot", "need a scraper", "need a script", "need automation help",
    "need api integration", "need a website", "need a landing page",
    "need a web developer", "need an ai assistant", "need a chatbot",
    "need a custom integration", "who can build", "can someone build",
    "hire someone to build", "quote for building",
]

# --- Неявная боль: автор описал свою проблему, исполнителя не просит (вес 3) ---
PAIN_POINT = [
    "manually copying data", "tired of doing this manually", "doing this by hand",
    "there has to be a better way", "wish there was a tool",
    "tracking this in a spreadsheet", "spending hours on this",
    "no api for this", "this process is broken", "manual data entry every",
    "my site is slow", "my site is broken", "keeps breaking",
    "losing track of leads", "re-entering the same data",
]

# --- Деньги: признак платящего заказчика (вес 2) ---
MONEY = [
    "budget", "hourly rate", "per hour", "fixed price", "paid", "will pay",
    "payment", "milestone", "usd", "invoice", "rate is", "compensation",
]

# --- Anti: автор САМ предлагает услуги (конкурент), а не ищет исполнителя (вес 0) ---
ANTI = [
    "for hire", "offering my", "offering services", "i am a developer",
    "im a developer", "available for work", "available for hire", "open to work",
    "hire me", "my services", "my portfolio", "my rates", "i specialize in",
    "looking for clients", "taking on new clients",
]

# --- Closed: заказ уже закрыт (вес 0) ---
CLOSED = [
    "position filled", "role filled", "found someone", "found a developer",
    "no longer looking", "no longer needed", "hired someone", "solved",
    "resolved", "got it sorted", "figured it out", "nevermind",
    "edit: found", "update: found",
]

# --- Noise: смежное, но не лид — самопиар, релизы, дискуссии (вес -4) ---
NOISE = [
    "check out my", "i built", "i just launched", "just launched", "sharing my",
    "roast my", "feedback on my", "looking for feedback", "would you use this",
    "im building", "show hn", "free trial", "discount code", "my course",
    "waitlist", "case study", "giveaway", "rate my", "my side project",
    "full-time position", "for equity", "equity only",
]

# Не-мой-профиль: задачи, которые физически есть на этих источниках,
# но не про разработку софта (вес -5).
OFF_NICHE = [
    "logo design", "graphic design", "video editing", "photo editing",
    "voice over", "transcription", "translation", "tutoring",
    "resume writing", "social media manager", "content writer", "copywriter",
    "proofreading", "3d model", "book cover",
]

# --- Aliases: свести варианты написания к одному (dict_type="alias", maps_to) ---
ALIASES = {
    "webscraping": "web scraping",
    "web-scraping": "web scraping",
    "chat bot": "chatbot",
    "chat-bot": "chatbot",
    "no code": "no-code",
    "nocode": "no-code",
    "freelancer": "freelance",
    "devs": "developer",
    "dev": "developer",
    "programmer": "developer",
    "engineer": "developer",
    "automating": "automate",
    "automations": "automation",
    "scrapers": "scraper",
    "scrape": "scraper",
    "bots": "bot",
    "scripts": "script",
    "websites": "website",
    "landing pages": "landing page",
    "chatbots": "chatbot",
    "workflows": "workflow",
    "spreadsheets": "spreadsheet",
    "integrations": "integration",
    "apis": "api",
}

# --- Веса правил (слой C/D скоринга) ---
RULE_WEIGHTS = {
    "has_sections": 3,
    "has_contact": 2,
    "has_money_pattern": 3,
    "has_bullets": 2,
    "long_len": 1,
    "short_len": -3,
    "no_body": -2,
    "no_stack": -3,
    # Мягкий сигнал "человек спрашивает, как решить X по нашей теме".
    # Намеренно маленький: сам по себе до порога уведомления не дотягивает,
    # его задача — дотолкать запись до серой зоны, где решение принимает LLM.
    "help_question": 2,
    "many_links": -2,
    "tag_hiring": 6,
    "tag_task": 5,
    "category_job_board": 3,
    "category_ai_automation": 1,
    "category_business_ops": 1,
    "author_reputation_bonus": 2,
    "noise_term_penalty": -4,
}
