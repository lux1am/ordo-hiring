/**
 * Форма отбора кандидатов на Google Apps Script.
 *
 * doGet отдаёт форму, doPost пишет анкету в лист «Данные» напрямую через
 * SpreadsheetApp — без сервисного аккаунта и без gspread: скрипт привязан к
 * таблице и работает от имени владельца.
 *
 * doPost принимает анкету и от статической страницы на своём домене
 * (dist/index.html): она приходит JSON-строкой, ответ ей — тоже JSON.
 *
 * Порядок колонок, тексты вопросов,CSS и JS телеметрии не пишутся здесь
 * руками: Headers.gs, Questions.gs и Index.html собираются из HEADERS.py,
 * questions.json и templates/form.html скриптом apps-script/build_gs.py.
 *
 * Зона записи — A..AH плюс AP (ответы о мотивации), AS (флаги), AU (статус)
 * и BC (метка размещения). Колонка AN (total_score) не трогается: там
 * формула таблицы.
 */

// Боевой адрес веб-приложения, тот самый, что раздан кандидатам.
// Впишите сюда URL развёртывания, оканчивающийся на /exec:
//   Развернуть → Управление развёртываниями → URL веб-приложения.
// Пусто — адрес будет запрошен у ScriptApp, но тот возвращает URL
// последнего развёртывания, а он не обязан совпадать с раздаваемым.
var WEB_APP_URL = 'https://script.google.com/macros/s/AKfycbw0gzVmKXuMQ8zf4_3MTdKxSLCLy1KrJy6kze18GCJtqAVsWpD8d5hkot6VVpqZZDEDXQ/exec';

var SHEET_NAME = 'Данные';
var HONEYPOT_FIELD = 'company_website';
// Ключ ошибки, не привязанной к конкретному полю: сбой записи, обрыв связи.
var FORM_ERROR_KEY = '_form';

// Пороги продублированы из app/config.py. Значения должны совпадать —
// расхождение означало бы, что одна и та же анкета получает разные флаги
// в зависимости от того, через какую форму она пришла.
var PASTE_PCT_THRESHOLD = 30;
var SPEED_CPM_THRESHOLD_DESKTOP = 400;
var SPEED_CPM_THRESHOLD_MOBILE = 700;
var SUSPICIOUS_MIN_LENGTH = 400;
var TAB_AWAY_SECONDS_THRESHOLD = 60;

// Обработчик анкет живёт в GitHub Actions, и после записи строки его просят
// проснуться сразу, не дожидаясь расписания. Токен в коде не хранится: он
// лежит в свойствах скрипта — см. apps-script/README.md, раздел «Токен
// GitHub в свойствах скрипта».
var GITHUB_REPO = 'lux1am/ordo-hiring';
var GITHUB_WORKFLOW = 'worker.yml';
var GITHUB_REF = 'main';
var GITHUB_TOKEN_PROPERTY = 'GITHUB_TOKEN';

// Метка размещения приходит из ?src= в адресе, то есть от кого угодно.
// Предел продублирован из app/source.py; совпадение чистки с Python и с
// общим JS формы закреплено тестом test_clean_source_matches_python.
var SOURCE_MAX_LEN = 40;

var STATUS_NEW = 'new';
var STATUS_DUPLICATE = 'duplicate';
var TZ_OFFSET_HOURS = 3;

// ============================== doGet =====================================

/**
 * Адрес веб-приложения. Пустая строка, если узнать его не удалось.
 *
 * ScriptApp.getService().getUrl() возвращает URL последнего развёртывания
 * и может бросить исключение при нехватке прав, поэтому он лишь запасной
 * источник, а обращение к нему обёрнуто в try.
 */
function webAppUrl() {
  if (WEB_APP_URL) return WEB_APP_URL;
  try {
    return ScriptApp.getService().getUrl() || '';
  } catch (err) {
    Logger.log('не удалось получить адрес веб-приложения: ' + err);
    return '';
  }
}

function doGet() {
  var t = HtmlService.createTemplateFromFile('Index');
  t.QUESTIONS = QUESTIONS;
  t.honeypot = HONEYPOT_FIELD;
  // Тот же адрес идёт в action формы: без него обычная отправка при
  // отключённом JavaScript никуда не уйдёт.
  t.formUrl = webAppUrl();
  return t.evaluate()
    .setTitle(QUESTIONS.title)
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

// ============================== doPost ====================================

/**
 * Проверка и запись анкеты. Общая точка для всех трёх способов отправки:
 * google.script.run из формы на Apps Script, JSON-запрос со статической
 * страницы и обычный POST без JavaScript — см. doPost.
 *
 * Возвращает { ok: true } либо { ok: false, errors: {поле: текст} }.
 * Ключ FORM_ERROR_KEY используется для ошибок, не привязанных к полю —
 * форма показывает их полосой над кнопкой отправки.
 */
function submitForm(payload) {
  var params = payload || {};

  // Приманка для ботов: поле скрыто стилями, человек его не видит.
  // Заполнено — отвечаем как обычно, но строку в таблице не занимаем.
  if (String(params[HONEYPOT_FIELD] || '').trim() !== '') {
    Logger.log('honeypot сработал — анкета не записана');
    return { ok: true };
  }

  var parsed = validate(params);
  if (!parsed.ok) {
    Logger.log('анкета не прошла валидацию: ' + JSON.stringify(parsed.errors));
    return { ok: false, errors: parsed.errors };
  }

  var telemetry = parseTelemetry(params.telemetry);
  var flags = telemetryFlags(telemetry).concat(profileFlags(parsed.data.profile));

  // Лог до записи: если SpreadsheetApp упадёт, анкету можно восстановить
  // из журнала выполнений.
  Logger.log('ПОЛУЧЕНА АНКЕТА ' + JSON.stringify({
    telegram: parsed.data.telegram, flags: flags
  }));

  try {
    writeSubmission(parsed.data, telemetry, flags);
  } catch (err) {
    Logger.log('не удалось записать анкету: ' + err);
    var errors = {};
    errors[FORM_ERROR_KEY] =
      'Не удалось сохранить анкету. Попробуйте отправить ещё раз через минуту.';
    return { ok: false, errors: errors };
  }

  // Строка записана — дальше кандидату уже ничем не навредишь. Просим
  // GitHub запустить обработчик сейчас, а не через 10 минут; не вышло —
  // остаётся в журнале, и анкету подберёт прогон по расписанию.
  try {
    triggerWorker();
  } catch (err) {
    Logger.log('не удалось запустить worker: ' + err);
  }
  return { ok: true };
}

/**
 * Просит GitHub Actions запустить worker.yml прямо сейчас:
 * POST /repos/<repo>/actions/workflows/worker.yml/dispatches с ref = main.
 *
 * Токен берётся из свойств скрипта по ключу GITHUB_TOKEN. Нет токена, нет
 * сети, ответил не тот код — пишем в журнал и возвращаем false: запуск по
 * событию ускоряет обработку, но не отвечает за неё. Ответственность на
 * расписании в worker.yml, оно остаётся запасным путём.
 *
 * Возвращает true, если GitHub принял запрос (204 без тела).
 */
function triggerWorker() {
  var token = PropertiesService.getScriptProperties()
    .getProperty(GITHUB_TOKEN_PROPERTY);
  if (!token) {
    Logger.log(
      'worker не запущен: в свойствах скрипта нет ' + GITHUB_TOKEN_PROPERTY
    );
    return false;
  }

  var url = 'https://api.github.com/repos/' + GITHUB_REPO +
    '/actions/workflows/' + GITHUB_WORKFLOW + '/dispatches';

  // muteHttpExceptions: код ответа разбираем сами. Исключение на 401 или 404
  // здесь ничего не даст — обрабатывать его всё равно нечем.
  var response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({ ref: GITHUB_REF }),
    muteHttpExceptions: true
  });

  var code = response.getResponseCode();
  if (code === 204) {
    Logger.log('worker запущен');
    return true;
  }
  Logger.log(
    'worker не запущен: HTTP ' + code + ' ' + response.getContentText()
  );
  return false;
}

/**
 * Разбор тела POST. Возвращает { params, json }.
 *
 * Статическая страница на своём домене (dist/index.html) шлёт анкету
 * JSON-строкой с Content-Type: text/plain. Тип выбран не для красоты: с
 * любым другим браузер сначала сделает preflight-запрос OPTIONS, а
 * Apps Script на него не отвечает и заголовков CORS не выдаёт — отправка
 * умерла бы, не начавшись. Поля такого запроса в e.parameter не попадают,
 * их видно только в e.postData.contents.
 *
 * Обычная отправка формы (запасной путь без JavaScript) по-прежнему
 * приходит как urlencoded, и её поля разобраны в e.parameter — этот путь
 * остаётся запасным и трогать его незачем.
 */
function parseBody(e) {
  var raw = e && e.postData && e.postData.contents;
  if (raw) {
    try {
      var parsed = JSON.parse(raw);
      // Тело urlencoded тоже лежит в contents, и «123» разобралось бы в
      // число. Анкета — всегда объект, остальное отдаём запасному пути.
      if (parsed && typeof parsed === 'object' && !(parsed instanceof Array)) {
        return { params: parsed, json: true };
      }
    } catch (err) {
      // Не JSON — значит обычная отправка формы. Это не ошибка.
    }
  }
  // Обычная отправка формы. У множественного выбора одно имя и несколько
  // значений: e.parameter хранит только первое, поэтому такие поля берутся
  // списком из e.parameters.
  var params = {};
  var single = (e && e.parameter) || {};
  var many = (e && e.parameters) || {};
  for (var key in single) {
    if (single.hasOwnProperty(key)) params[key] = single[key];
  }
  for (var name in many) {
    if (many.hasOwnProperty(name) && many[name] && many[name].length > 1) {
      params[name] = many[name];
    }
  }
  return { params: params, json: false };
}

/**
 * Приём анкеты обычным POST.
 *
 * Два вызывающих, и отвечать им надо по-разному. Статическая страница ждёт
 * JSON { ok, errors } и рисует ошибки под полями сама. У формы без
 * JavaScript показывать результат на месте некому, поэтому ей уходит
 * HTML-страница. Проверки при этом одни и те же — см. submitForm.
 */
function doPost(e) {
  var body = parseBody(e);
  var result = submitForm(body.params);

  if (body.json) {
    return jsonResponse(result);
  }
  if (result.ok) {
    return renderThanks();
  }
  return renderError(joinErrors(result.errors));
}

/** Ответ статической странице: то же самое, что вернул бы submitForm. */
function jsonResponse(result) {
  return ContentService
    .createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}

/** Ошибки по полям — в одну строку, для страницы без формы. */
function joinErrors(errors) {
  var out = [];
  for (var key in errors) {
    if (errors.hasOwnProperty(key)) out.push(errors[key]);
  }
  return out.join('; ');
}

// ====================== Ответы с вариантами ===============================
//
// Зеркало app/profile.py и app/checks.py. Тексты ошибок и формат ячейки
// обязаны совпадать до знака: одна и та же анкета, пришедшая через FastAPI
// и через Apps Script, должна лечь в колонку одинаково и получить те же
// флаги, иначе обработчик разберёт её только у половины кандидатов.

// Между вариантом ответа и уточнением к нему. Тире с пробелами: внутри
// самих вариантов есть короткое тире без пробелов («10–14»).
var STOP_SEP = ' — ';

var EMPTY_CHOICE_RU = 'Выберите один вариант';
var UNKNOWN_CHOICE_RU = 'Такого варианта в анкете нет';
var EMPTY_TEXT_RU = 'Заполните это поле';
var TOO_LONG_RU = 'Слишком длинный ответ';
var FEW_CHOICES_RU = 'Выберите хотя бы один вариант';
var MANY_CHOICES_RU = 'Выберите не больше двух вариантов';
var OTHER_REQUIRED_RU = 'Напишите свой вариант';
var PLACE_REQUIRED_RU = 'Расставьте все четыре места';
var PLACE_UNIQUE_RU = 'Каждое место должно быть занято один раз';

/** «Да» + «Ваш часовой пояс» + «МСК+2» → «Да — Ваш часовой пояс: МСК+2». */
function formatStop(value, extraLabel, extra) {
  value = String(value || '').trim();
  extra = String(extra || '').trim();
  if (!extra) return value;
  return value + STOP_SEP + extraLabel + ': ' + extra;
}

function asList(value) {
  if (value === null || value === undefined) return [];
  if (value instanceof Array) return value;
  return [value];
}

/** Один вариант из списка: стоп-вопрос или вопрос-флаг с типом single. */
function validateChoice(q, params, errors) {
  var value = String(params[q.field] || '').trim();
  if (!value) {
    errors[q.field] = EMPTY_CHOICE_RU;
    return '';
  }
  if (q.options.indexOf(value) === -1) {
    errors[q.field] = UNKNOWN_CHOICE_RU;
    return '';
  }
  return value;
}

/** Множественный выбор: от min_choices до max_choices вариантов. */
function validateMulti(q, params, errors, extras) {
  var raw = asList(params[q.field]);
  var chosen = [];
  for (var i = 0; i < raw.length; i++) {
    var value = String(raw[i]).trim();
    if (value) chosen.push(value);
  }

  for (var j = 0; j < chosen.length; j++) {
    if (q.options.indexOf(chosen[j]) === -1) {
      errors[q.field] = UNKNOWN_CHOICE_RU;
      return [];
    }
  }
  if (chosen.length < q.min_choices) {
    errors[q.field] = FEW_CHOICES_RU;
    return [];
  }
  if (chosen.length > q.max_choices) {
    errors[q.field] = MANY_CHOICES_RU;
    return [];
  }

  if (q.other_option && chosen.indexOf(q.other_option) !== -1) {
    var text = normalize(params[q.field + '_other']);
    if (!text) {
      errors[q.field + '_other'] = OTHER_REQUIRED_RU;
    } else if (text.length > q.other_max_length) {
      errors[q.field + '_other'] = TOO_LONG_RU;
    } else {
      // Ключ появляется только когда выбран сам вариант — как в profile.py.
      extras[q.field + '_other'] = text;
    }
  }
  return chosen;
}

/** Ранжирование: каждое место занято ровно один раз. */
function validateRank(q, params, errors) {
  var out = {};
  var seen = {};
  var unique = 0;

  for (var i = 0; i < q.rows.length; i++) {
    var name = q.rows[i].field;
    var value = String(params[name] || '').trim();
    if (q.places.indexOf(value) === -1) {
      errors[q.field] = PLACE_REQUIRED_RU;
      return {};
    }
    out[name] = value;
    if (!seen[value]) {
      seen[value] = true;
      unique++;
    }
  }
  if (unique !== q.places.length) {
    errors[q.field] = PLACE_UNIQUE_RU;
    return {};
  }
  return out;
}

// --- Флаги по вопросам о мотивации ---------------------------------------
//
// Продублировано из app/checks.py. Варианты названы теми же строками, что в
// questions.json: опечатка в коротком тире молча сделала бы правило
// нерабочим, поэтому строки копируются, а не переписываются.

var MOTIVE_AVOIDANCE = ['Уйти на удалёнку', 'Сменить сферу'];
var INCOME_LOW = 'до 50 000 ₽';
var INCOME_UNREAL = 'более 150 000 ₽';
var MEETINGS_UNDERESTIMATE = 'Легко достижимо';
var MEETINGS_PESSIMIST = 'Нереально';
var GOALS_UNKNOWN = 'Не знаю';
var RANK_INCOME = 'flag_rank_income';
var RANK_STABILITY = 'flag_rank_stability';
var INCOME_LOW_PLACES = ['3', '4'];

/**
 * Ячейка ai_markers: зеркало profile.pack_markers. Признаки машинного
 * текста пишет обработчик, ответы кандидата — форма, и живут они в одной
 * колонке: отдельной под ответы в раскладке нет.
 */
function packMarkers(profile, markers) {
  var hasProfile = false;
  for (var key in profile) {
    if (profile.hasOwnProperty(key)) { hasProfile = true; break; }
  }
  if (!hasProfile && (!markers || !markers.length)) return '';
  return JSON.stringify({ markers: markers || [], profile: profile || {} });
}

/** Повторяет app/checks.py: profile_flags. Баллов эти флаги не меняют. */
function profileFlags(profile) {
  var flags = [];

  var motive = profile.flag_motive || [];
  if (motive.length === 1 && MOTIVE_AVOIDANCE.indexOf(motive[0]) !== -1) {
    addFlag(flags, 'MOTIVATION_AVOIDANCE');
  }

  var income = profile.flag_income || '';
  if (income === INCOME_LOW) {
    addFlag(flags, 'INCOME_LOW');
  } else if (income === INCOME_UNREAL) {
    addFlag(flags, 'INCOME_UNREAL');
  }

  var rank = profile.flag_rank || {};
  if (rank[RANK_STABILITY] === '1') addFlag(flags, 'STABILITY_FIRST');
  if (INCOME_LOW_PLACES.indexOf(rank[RANK_INCOME]) !== -1) {
    addFlag(flags, 'INCOME_NOT_PRIORITY');
  }

  var meetings = profile.flag_meetings || '';
  if (meetings === MEETINGS_UNDERESTIMATE) {
    addFlag(flags, 'UNDERESTIMATE');
  } else if (meetings === MEETINGS_PESSIMIST) {
    addFlag(flags, 'PESSIMIST');
  }

  if ((profile.flag_goals || '') === GOALS_UNKNOWN) addFlag(flags, 'NO_GOALS');

  return flags;
}

// =========================== Валидация ====================================

var TELEGRAM_RE = /^@?[A-Za-z0-9_]{4,32}$/;
var URL_RE = /^https?:\/\/\S+\.\S+/i;

/**
 * Схлопывает пробелы и переносы. Длина меряется именно после этого —
 * в браузере счётчик под полем считает так же, иначе кандидат видел бы
 * «80/80», а сервер отвечал бы «слишком короткий».
 */
function normalize(text) {
  return String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
}

/**
 * Метка размещения в том виде, в каком её можно положить в таблицу.
 *
 * Оставляем латиницу, цифры, подчёркивание и дефис, приводим к нижнему
 * регистру и обрезаем по длине. Всё прочее выбрасывается: метка —
 * техническое слово из листа «Размещения», а не текст от человека.
 * Чистка та же, что в app/source.py и в общем JS формы.
 */
function cleanSource(raw) {
  return String(raw == null ? '' : raw)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]/g, '')
    .slice(0, SOURCE_MAX_LEN);
}

/**
 * Серверная валидация. Последний рубеж: форма проверяет то же самое до
 * отправки, но запрос может прийти и в обход неё.
 *
 * Возвращает { ok, errors, data }, где errors — объект «имя поля → текст».
 * Форма раскладывает эти тексты под соответствующими полями; doPost, у
 * которого полей нет, склеивает их в одну строку.
 */
function validate(params) {
  var errors = {};
  var data = {};

  var fio = normalize(params.fio);
  if (fio.length < 5 || fio.split(' ').length < 2) {
    errors.fio = 'Укажите фамилию и имя полностью';
  } else if (fio.length > 200) {
    errors.fio = 'Слишком длинное ФИО';
  }
  data.fio = fio;

  var tg = String(params.telegram || '').trim()
    .replace(/^(https?:\/\/)?(t\.me|telegram\.me)\//i, '');
  if (!TELEGRAM_RE.test(tg)) {
    errors.telegram = 'Telegram укажите в виде @username';
    data.telegram = tg;
  } else {
    data.telegram = '@' + tg.replace(/^@/, '');
  }

  var url = String(params.resume_url || '').trim();
  if (!URL_RE.test(url)) {
    errors.resume_url = 'Ссылка на резюме должна начинаться с http:// или https://';
  } else if (url.length > 2000) {
    errors.resume_url = 'Слишком длинная ссылка';
  }
  data.resume_url = url;

  // Метка размещения ошибкой быть не может: её не вводят руками, и анкета
  // без метки — обычное дело (прямой переход по ссылке без ?src=).
  data.source = cleanSource(params.source);

  data.pdn_consent = String(params.pdn_consent || '') !== '';
  if (!data.pdn_consent) {
    errors.pdn_consent =
      'Без согласия на обработку персональных данных анкету не принять';
  }

  // Стоп-вопросы. Уточнение к вопросу — часовой пояс — приписывается к
  // ячейке того же вопроса: новых колонок под него в листе нет.
  data.stops = {};
  for (var i = 0; i < QUESTIONS.stop_questions.length; i++) {
    var sq = QUESTIONS.stop_questions[i];
    var choice = validateChoice(sq, params, errors);
    if (!sq.extra) {
      data.stops[sq.field] = choice;
      continue;
    }
    var extra = normalize(params[sq.extra.field]);
    if (!extra) {
      errors[sq.extra.field] = EMPTY_TEXT_RU;
    } else if (extra.length > sq.extra.max_length) {
      errors[sq.extra.field] = TOO_LONG_RU;
    }
    data.stops[sq.field] = formatStop(choice, sq.extra.label, extra);
  }

  // Вопросы-флаги. В лист они не пишутся: из них получаются флаги, а те
  // ложатся в колонку AS вместе с флагами телеметрии.
  data.profile = {};
  var extras = {};
  for (var f = 0; f < QUESTIONS.flag_questions.length; f++) {
    var fq = QUESTIONS.flag_questions[f];
    if (fq.type === 'multi') {
      data.profile[fq.field] = validateMulti(fq, params, errors, extras);
    } else if (fq.type === 'rank') {
      data.profile[fq.field] = validateRank(fq, params, errors);
    } else {
      data.profile[fq.field] = validateChoice(fq, params, errors);
    }
  }
  for (var extraKey in extras) {
    if (extras.hasOwnProperty(extraKey)) data.profile[extraKey] = extras[extraKey];
  }

  data.answers = {};
  for (var j = 0; j < QUESTIONS.open_questions.length; j++) {
    var oq = QUESTIONS.open_questions[j];
    var text = normalize(params[oq.field]);
    data.answers[oq.field] = text;

    if (!text) {
      errors[oq.field] = 'Ответьте на этот вопрос';
      continue;
    }
    var min = oq.min_length || 0;
    if (text.length < min) {
      errors[oq.field] =
        'Ответ слишком короткий: ' + text.length + ' из ' + min +
        ' знаков минимум';
    } else if (oq.max_length && text.length > oq.max_length) {
      errors[oq.field] = 'Не больше ' + oq.max_length + ' знаков';
    }
  }

  return { ok: !hasKeys(errors), errors: errors, data: data };
}

function hasKeys(obj) {
  for (var key in obj) {
    if (obj.hasOwnProperty(key)) return true;
  }
  return false;
}

// =========================== Телеметрия ===================================

function parseTelemetry(raw) {
  var t = { is_mobile: false, tab_switches: 0, total_time_sec: 0, fields: {} };
  try {
    var parsed = JSON.parse(raw || '{}');
    t.is_mobile = !!parsed.is_mobile;
    t.tab_switches = clamp(parsed.tab_switches, 0, 10000);
    t.total_time_sec = clamp(parsed.total_time_sec, 0, 86400);
    var fields = parsed.fields || {};
    for (var key in fields) {
      if (!fields.hasOwnProperty(key)) continue;
      var f = fields[key] || {};
      var length = clamp(f.length, 0, 1000000);
      t.fields[key] = {
        length: length,
        pasted_chars: clamp(f.pasted_chars, 0, length),
        speed_cpm: clamp(f.speed_cpm, 0, 100000),
        backspaces: clamp(f.backspaces, 0, 1000000),
        away_before_input_sec: clamp(f.away_before_input_sec, 0, 86400)
      };
    }
  } catch (err) {
    // Телеметрия приходит от кандидата. Кривая не должна стоить ему анкеты.
    Logger.log('телеметрия не разобралась, продолжаю с пустой: ' + err);
  }
  return t;
}

function clamp(value, lo, hi) {
  var n = Number(value);
  if (!isFinite(n)) return lo;
  return Math.max(lo, Math.min(n, hi));
}

function fieldTelemetry(t, name) {
  return t.fields[name] || {
    length: 0, pasted_chars: 0, speed_cpm: 0, backspaces: 0, away_before_input_sec: 0
  };
}

function pastePct(m) {
  if (!m.length) return 0;
  return Math.min(100, Math.round(m.pasted_chars / m.length * 1000) / 10);
}

/** Повторяет app/checks.py: telemetry_flags. Мобильные проверяются мягче. */
function telemetryFlags(t) {
  var flags = [];
  if (t.is_mobile) flags.push('MOBILE');
  var speedLimit = t.is_mobile
    ? SPEED_CPM_THRESHOLD_MOBILE
    : SPEED_CPM_THRESHOLD_DESKTOP;

  for (var i = 0; i < QUESTIONS.open_questions.length; i++) {
    var m = fieldTelemetry(t, QUESTIONS.open_questions[i].field);

    if (pastePct(m) > PASTE_PCT_THRESHOLD) {
      addFlag(flags, 'PASTED');
    } else if (m.speed_cpm > speedLimit) {
      addFlag(flags, 'PASTED');
    }

    // На мобильных keydown по Backspace приходит нестабильно — правило снято.
    if (!t.is_mobile && m.backspaces === 0 && m.length > SUSPICIOUS_MIN_LENGTH) {
      addFlag(flags, 'SUSPICIOUS');
    }

    if (m.away_before_input_sec > TAB_AWAY_SECONDS_THRESHOLD) {
      addFlag(flags, 'TAB_SWITCH');
    }
  }
  return flags;
}

function addFlag(flags, flag) {
  if (flags.indexOf(flag) === -1) flags.push(flag);
}

// ============================= Запись =====================================

function getSheet() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  if (!sheet) {
    throw new Error('в таблице нет листа «' + SHEET_NAME + '»');
  }
  return sheet;
}

function writeSubmission(data, telemetry, flags) {
  // Блокировка на весь цикл «найти строку — записать»: без неё две
  // одновременные отправки получили бы один и тот же номер строки.
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var sheet = getSheet();
    var duplicate = telegramExists(sheet, data.telegram);
    var row = allocateRow(sheet);

    sheet.getRange(row, 1, 1, COL_FORM_END)
      .setValues([buildFormRow(data, telemetry)]);
    // Ответы о мотивации кладём в AP сразу: отдельной колонки под них нет,
    // а в анкетной зоне A..AH их не бывает — обработчик их уже не увидит.
    sheet.getRange(row, COL_AI_MARKERS).setValue(packMarkers(data.profile, []));
    sheet.getRange(row, COL_FLAGS).setValue(flags.join(', '));
    sheet.getRange(row, COL_STATUS)
      .setValue(duplicate ? STATUS_DUPLICATE : STATUS_NEW);
    // Метка идёт отдельной ячейкой: в анкетной зоне A..AH места под неё
    // нет, а сдвигать зону нельзя — на её границы опирается формула в AN.
    sheet.getRange(row, COL_SOURCE).setValue(data.source);

    if (duplicate) {
      Logger.log('строка ' + row + ': дубль по телеграму ' + data.telegram);
    } else {
      Logger.log('анкета записана в строку ' + row +
                 ', метка: ' + (data.source || 'нет') +
                 ', флаги: ' + (flags.join(', ') || 'нет'));
    }
    SpreadsheetApp.flush();
  } finally {
    lock.releaseLock();
  }
}

/**
 * Следующая свободная строка — по колонке A.
 *
 * getLastRow() здесь не годится: формула в AN размечена на 300 строк вперёд,
 * и для таблицы эти строки не пустые — getLastRow() вернул бы 301 сразу.
 */
function allocateRow(sheet) {
  var maxRows = sheet.getMaxRows();
  var column = sheet.getRange(1, 1, maxRows, 1).getValues();
  var last = 0;
  for (var i = 0; i < column.length; i++) {
    if (String(column[i][0]).trim() !== '') last = i + 1;
  }
  var row = Math.max(last + 1, FIRST_DATA_ROW);

  if (row > FORMULA_TOPUP_FROM_ROW) {
    ensureTotalFormula(sheet, row);
  }
  return row;
}

/** Дописывает формулу total_score в AN — так лимит в 300 строк снимается сам. */
function ensureTotalFormula(sheet, row) {
  if (row > LAST_PREPARED_ROW) {
    Logger.log('строка ' + row + ' вне размеченных ' + LAST_PREPARED_ROW +
               ' — формула total_score дописывается скриптом');
  }
  try {
    sheet.getRange(row, COL_TOTAL_SCORE)
      .setFormula(TOTAL_SCORE_FORMULA.replace(/\{row\}/g, String(row)));
  } catch (err) {
    Logger.log('не удалось дописать формулу total_score в строку ' + row + ': ' + err);
  }
}

function telegramExists(sheet, telegram) {
  var needle = String(telegram).replace(/^@/, '').toLowerCase();
  if (!needle) return false;
  var maxRows = sheet.getMaxRows();
  var column = sheet.getRange(1, COL_TELEGRAM, maxRows, 1).getValues();
  for (var i = FIRST_DATA_ROW - 1; i < column.length; i++) {
    if (String(column[i][0]).trim().replace(/^@/, '').toLowerCase() === needle) {
      return true;
    }
  }
  return false;
}

/** 34 значения зоны A..AH, строго в порядке HEADERS. */
function buildFormRow(data, telemetry) {
  var row = [
    Utilities.getUuid().replace(/-/g, '').substring(0, 12),
    Utilities.formatDate(
      new Date(), 'GMT+' + TZ_OFFSET_HOURS, 'yyyy-MM-dd HH:mm:ss'
    ),
    data.fio,
    data.telegram,
    data.resume_url,
    data.pdn_consent ? 'Да' : 'Нет'
  ];

  var stops = QUESTIONS.stop_questions;
  for (var i = 0; i < stops.length; i++) row.push(data.stops[stops[i].field] || '');

  var open = QUESTIONS.open_questions;
  for (var j = 0; j < open.length; j++) row.push(data.answers[open[j].field] || '');
  for (var k = 0; k < open.length; k++) {
    row.push(pastePct(fieldTelemetry(telemetry, open[k].field)));
  }
  for (var m = 0; m < open.length; m++) {
    row.push(Math.round(fieldTelemetry(telemetry, open[m].field).speed_cpm * 10) / 10);
  }
  for (var n = 0; n < open.length; n++) {
    row.push(fieldTelemetry(telemetry, open[n].field).backspaces);
  }

  row.push(telemetry.tab_switches);
  row.push(Math.round(telemetry.total_time_sec));

  if (row.length !== COL_FORM_END) {
    throw new Error('ожидалось ' + COL_FORM_END + ' значений, собрано ' + row.length);
  }
  return row;
}

// ============================== Ответы ====================================

function renderThanks() {
  return page(
    'Анкета отправлена',
    '<h1>Спасибо, анкета отправлена</h1>' +
    '<p>Мы изучим ваши ответы и свяжемся с вами в Telegram.</p>' +
    '<p>Отправлять анкету повторно не нужно.</p>'
  );
}

function renderError(message) {
  // history.back() отсюда не работает: Apps Script рендерит приложение в
  // изолированном iframe, и назад отматывается история самого iframe.
  // Нужна прямая ссылка, обязательно с target="_top" — без него страница
  // откроется внутри того же iframe.
  var url = webAppUrl();

  // Адрес неизвестен — обходимся текстом. Страница ошибки обязана
  // отрисоваться при любых обстоятельствах: она и так последнее, что
  // видит кандидат.
  var back = url
    ? '<p><a href="' + escapeHtml(url) + '" target="_top">Открыть анкету заново</a></p>'
    : '<p>Вернитесь на страницу анкеты и заполните заново</p>';

  return page(
    'Проверьте анкету',
    '<h1>Анкета не отправлена</h1>' +
    '<p class="err">' + escapeHtml(message) + '</p>' +
    back
  );
}

function page(title, body) {
  var html =
    '<!doctype html><html lang="ru"><head><base target="_top"><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<title>' + escapeHtml(title) + '</title><style>' +
    'body{margin:0;padding:24px 16px;background:#f5f5f4;color:#1c1917;' +
    'font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;' +
    'display:flex;align-items:center;justify-content:center;min-height:100vh}' +
    '.card{max-width:700px;width:100%;background:#fff;border:1px solid #d6d3d1;' +
    'border-radius:8px;padding:40px 32px;text-align:center}' +
    'h1{font-size:24px;margin:0 0 12px;font-weight:650}' +
    'p{color:#57534e;margin:0 0 8px}p:last-child{margin-bottom:0}' +
    '.err{color:#991b1b}a{color:#1c1917}' +
    '@media(max-width:560px){.card{padding:28px 20px}h1{font-size:21px}}' +
    '</style></head><body><div class="card">' + body + '</div></body></html>';

  return HtmlService.createHtmlOutput(html)
    .setTitle(title)
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ========================== Самопроверка ==================================

/**
 * Запустите вручную из редактора после первой установки.
 * Проверяет доступ к листу и совпадение шапки с HEADERS.
 */
function checkSetup() {
  var sheet = getSheet();
  var actual = sheet.getRange(1, 1, 1, HEADERS.length).getValues()[0];
  var problems = [];
  for (var i = 0; i < HEADERS.length; i++) {
    if (String(actual[i]).trim() !== HEADERS[i]) {
      problems.push(
        'колонка ' + (i + 1) + ': ожидалось «' + HEADERS[i] +
        '», в таблице «' + actual[i] + '»'
      );
    }
  }
  if (problems.length) {
    Logger.log('ШАПКА РАСХОДИТСЯ:\n' + problems.join('\n'));
  } else {
    Logger.log('шапка листа совпадает с HEADERS (' + HEADERS.length + ' колонок)');
  }
  Logger.log('следующая свободная строка: ' + allocateRow(sheet));
  Logger.log('адрес формы: ' + (webAppUrl() || 'НЕИЗВЕСТЕН — впишите WEB_APP_URL'));
  Logger.log(
    PropertiesService.getScriptProperties().getProperty(GITHUB_TOKEN_PROPERTY)
      ? 'токен GitHub: найден в свойствах скрипта'
      : 'токен GitHub: не задан — обработчик будет ждать расписания'
  );
  return problems.length === 0;
}
