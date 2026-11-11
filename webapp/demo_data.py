"""Offline, repeatable fixtures for an existing disposable .test account."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .runtime import Runtime
from .store import Store

SEED = "demo-correspondence-v1"
MAILBOX = "purchasing@gmail.example.test"
ANCHOR = datetime(2026, 11, 11, 11, 30, tzinfo=timezone(timedelta(hours=3)))

VENDORS = [
    ("СтальКомплект", "Металлопрокат", "Москва"), ("Северный прокат", "Металлопрокат", "Тверь"),
    ("МеталлСнаб Центр", "Металлопрокат", "Тула"), ("ПрофильТрейд", "Металлопрокат", "Рязань"),
    ("ТрубаПоставка", "Металлопрокат", "Владимир"), ("ПромСталь", "Металлопрокат", "Калуга"),
    ("Кабельная линия", "Электрика", "Москва"), ("ЭлектроОпт", "Электрика", "Подольск"),
    ("СветПроект", "Электрика", "Мытищи"), ("ЭнергоКомплект", "Электрика", "Казань"),
    ("Облачные решения", "Google Workspace", "Москва"), ("Офис в облаке", "Google Workspace", "Санкт-Петербург"),
    ("ИТ Поддержка", "Google Workspace", "Екатеринбург"), ("Командные сервисы", "Google Workspace", "Москва"),
    ("КрепёжПрофи", "Крепёж", "Москва"), ("Болт и Гайка", "Крепёж", "Чехов"),
    ("МонтажКомплект", "Крепёж", "Тула"), ("ПромМетиз", "Крепёж", "Орёл"),
    ("БетонПоставка", "Стройматериалы", "Москва"), ("СтройОснова", "Стройматериалы", "Домодедово"),
    ("Бетонный завод Восток", "Стройматериалы", "Балашиха"), ("СмесьКомплект", "Стройматериалы", "Клин"),
    ("ОфисМаркет", "Канцтовары", "Москва"), ("Бумага и Дело", "Канцтовары", "Химки"),
]

SCENARIOS = [
    ("Труба и металлопрокат — склад Северный", list(range(6)), "active", "Профильная труба 80×80×4", "т", 12),
    ("Кабель и освещение — офис", list(range(6, 10)), "ready", "Кабель ВВГнг-LS 3×2,5", "м", 800),
    ("Google Workspace — продление лицензий", list(range(10, 14)), "active", "Google Workspace, лицензии на 12 месяцев", "шт", 25),
    ("Крепёж — объект Южный", list(range(14, 18)), "closed", "Болт М12×80 с гайкой и шайбой", "компл", 1200),
    ("Бетон и растворы — фундамент", list(range(18, 22)), "ready", "Бетон М300 с доставкой", "м³", 48),
    ("Бумага и канцтовары — ноябрь", [22, 23, 10, 11], "draft", "Бумага офисная А4, 500 листов", "пач", 60),
    ("Повторная поставка металла", [0, 2, 3, 4], "active", "Лист стальной 4 мм", "т", 5),
]


def _sheet(path: Path, title: str, headers: list[str], rows: list[list]):
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = "Демонстрация"
    sheet.append(["ТЕСТОВЫЕ ДАННЫЕ — не использовать для оплаты"])
    sheet.append([title])
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    sheet.freeze_panes = "C4"
    sheet.auto_filter.ref = f"A3:{sheet.cell(3,len(headers)).column_letter}{sheet.max_row}"
    for cell in sheet[3]:
        cell.fill = PatternFill("solid", fgColor="2457A7")
        cell.font = Font(color="FFFFFF", bold=True)
    for index in range(1, len(headers) + 1):
        sheet.column_dimensions[sheet.cell(3,index).column_letter].width = 20 if index != 2 else 55
    for row in sheet.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    sheet.row_dimensions[1].height = 32
    sheet.row_dimensions[2].height = 28
    book.save(path)


def _eml(path: Path, sender: str, to: str, subject: str, body: str, mid: str,
         when: datetime, parent: str | None = None, files: tuple[Path, ...] = ()):
    message = EmailMessage(policy=policy.SMTP)
    message["From"], message["To"], message["Subject"] = sender, to, subject
    message["Message-ID"], message["Date"] = mid, format_datetime(when)
    message["X-Demo-Fixture"] = SEED
    if parent:
        message["In-Reply-To"] = parent
        message["References"] = parent
    message.set_content(body)
    for file in files:
        subtype = "vnd.openxmlformats-officedocument.spreadsheetml.sheet" if file.suffix == ".xlsx" else "octet-stream"
        message.add_attachment(file.read_bytes(), maintype="application", subtype=subtype, filename=file.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(message.as_bytes())


def seed_demo(store: Store, email: str = "demo@example.test") -> dict:
    if not email.lower().endswith(".test"):
        raise ValueError("Демо-данные можно добавлять только в тестовый аккаунт с доменом .test")
    with store.connect() as connection:
        user = connection.execute("SELECT id FROM users WHERE email=?", (email.lower(),)).fetchone()
    if user is None:
        raise ValueError("Тестовый аккаунт не найден. Сначала создайте его.")
    runtime = Runtime(store)
    service = runtime.service(user["id"])
    db, paths = service.db, service.paths
    if db.get_setting(SEED) == "done":
        return {"already_seeded": True}
    db.backup_to(paths.backups / f"before-demo-{uuid.uuid4().hex[:8]}.db")
    counts = {"suppliers": 0, "campaigns": 0, "incoming": 0, "outgoing": 0, "attachments": 0}
    summaries = []
    stamp = lambda moment: moment.isoformat(timespec="seconds")
    with db.connect() as connection:
        # One transaction keeps partial fixture data out of the user's lists.
        connection.execute("BEGIN IMMEDIATE")
        vendor_ids = []
        for index, (name, category, city) in enumerate(VENDORS):
            address = f"vendor{index+1:02d}@gmail.example.test" if index % 2 == 0 else f"sales@vendor{index+1:02d}.example.test"
            cursor = connection.execute("INSERT INTO suppliers(name,email,notes,phones_json,contact_person,address,website,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (name + " (демо)", address, f"{SEED}. Вымышленная компания для проверки интерфейса.",
                json.dumps([f"+7 (000) 000-00-{index+1:02d}"]), ["Анна", "Михаил", "Елена", "Сергей"][index % 4],
                f"{city}, Демонстрационная улица, {index+1}", f"https://vendor{index+1:02d}.example.test",
                stamp(ANCHOR-timedelta(days=40)), stamp(ANCHOR)))
            vendor_ids.append((cursor.lastrowid, address))
            connection.execute("INSERT INTO supplier_categories VALUES(?,?)", (cursor.lastrowid, category))
            counts["suppliers"] += 1

        for scenario, (name, indexes, state, product, unit, quantity) in enumerate(SCENARIOS):
            created = ANCHOR - timedelta(days=10-scenario, hours=2)
            code = db._next_campaign_code(connection)
            subject = f"Запрос предложения: {name} [{code}]"
            body = (f"Добрый день!\n\nПросим подготовить предложение: {product}, {quantity} {unit}. "
                    "Заявка во вложении. Укажите цену с НДС, сроки поставки и условия оплаты.\n\n"
                    "С уважением,\nАнна, отдел закупок\n\nДемонстрационная переписка. Письмо не отправлялось.")
            cursor = connection.execute("INSERT INTO campaigns(code,name,subject,body,deadline,status,created_at,sent_at,last_checked_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (code, "Демо · " + name, subject, body,
                (created+timedelta(days=4)).date().isoformat(), state, stamp(created),
                None if state=="draft" else stamp(created+timedelta(hours=1)), None if state=="draft" else stamp(ANCHOR)))
            campaign_id = cursor.lastrowid
            counts["campaigns"] += 1
            root = paths.campaigns / code
            request = root / "outgoing" / "attachments" / "Заявка.xlsx"
            _sheet(request, name, ["№", "Наименование", "Ед.", "Количество"], [[1,product,unit,quantity], [2,"Дополнительная позиция по заявке",unit,quantity//2 or 1]])
            connection.execute("UPDATE campaigns SET request_path=? WHERE id=?", (str(request), campaign_id))
            connection.execute("INSERT INTO campaign_attachments(campaign_id,filename,path,size,sha256,created_at) VALUES(?,?,?,?,?,?)",
                (campaign_id,request.name,str(request),request.stat().st_size,hashlib.sha256(request.read_bytes()).hexdigest(),stamp(created)))

            def incoming(recipient_id, address, turn, text, when, parent, attach=False, review=False):
                uid = scenario*1000 + (recipient_id or 900)*10 + turn
                mid = f"<{SEED}-{scenario}-{recipient_id}-{turn}-in@example.test>"
                eml = root / "incoming" / f"{uid}.eml"
                attachments = []
                if attach:
                    quote = root / "incoming" / "attachments" / f"Счёт_{uid}.xlsx"
                    price = 1600 + scenario*850 + (recipient_id or 0)*65
                    _sheet(quote, f"Предложение к {code}", ["№","Наименование","Ед.","Количество","Цена с НДС, ₽","Сумма, ₽"],
                        [[1,product,unit,quantity,price,quantity*price], [2,"Доставка","усл",1,2500,2500]])
                    attachments.append(quote)
                    terms = quote.with_name(f"Условия_{uid}.txt")
                    terms.write_text("ТЕСТОВОЕ ПРЕДЛОЖЕНИЕ\nОплата: 30% аванс, 70% после поставки.\nСрок: 5 рабочих дней.\nДоставка включена в предложение.\n",encoding="utf-8")
                    attachments.append(terms)
                _eml(eml,address,MAILBOX,"Re: "+subject,text,mid,when,parent,tuple(attachments))
                cursor = connection.execute("INSERT INTO incoming_messages(mailbox,folder,uid_validity,imap_uid,campaign_id,recipient_id,message_id,in_reply_to,"
                    "sender_email,subject,received_at,body_text,raw_eml_path,match_method,needs_review,created_at,processed_at,opened_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (MAILBOX,"INBOX",SEED,uid,None if review else campaign_id,recipient_id,
                    mid,parent,address,"Re: "+subject,stamp(when),text,str(eml),"unmatched" if review else "reply_headers",int(review),
                    stamp(when),stamp(when),stamp(when+timedelta(minutes=10)) if uid%3 else None))
                incoming_id = cursor.lastrowid
                counts["incoming"] += 1
                for file in attachments:
                    connection.execute("INSERT INTO attachments(incoming_message_id,filename,content_type,size,sha256,path,is_allowed,is_selected,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (incoming_id,file.name,"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if file.suffix==".xlsx" else "text/plain",
                        file.stat().st_size,hashlib.sha256(file.read_bytes()).hexdigest(),str(file),int(file.suffix==".xlsx"),int(file.suffix==".xlsx"),stamp(when)))
                    counts["attachments"] += 1
                return mid,incoming_id

            def outgoing(recipient_id,address,turn,text,when,parent=None,reply_to=None,kind="reply",delivery="sent"):
                mid = f"<{SEED}-{scenario}-{recipient_id}-{turn}-out@example.test>"
                eml = root / "outgoing" / "messages" / f"{recipient_id}-{turn}.eml"
                _eml(eml,MAILBOX,address,subject if kind=="campaign" else "Re: "+subject,text,mid,when,parent,
                     (request,) if kind=="campaign" else ())
                connection.execute("INSERT INTO outgoing_messages(campaign_id,recipient_id,message_id,subject,sent_at,raw_eml_path,delivery_status,kind,to_email,body,attachments_json,reply_to_incoming_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (campaign_id,recipient_id,mid,subject if kind=="campaign" else "Re: "+subject,
                    stamp(when),str(eml),delivery,kind,address,text,"[]",reply_to))
                counts["outgoing"] += 1
                return mid

            for offset,index in enumerate(indexes):
                supplier_id,address = vendor_ids[index]
                status = ("pending" if state=="draft" else "declined" if state in {"ready","closed"} and offset==len(indexes)-1
                          else "sent" if state=="active" and offset==len(indexes)-1
                          else "reply_without_files" if state=="active" and offset==len(indexes)-2 else "files_received")
                if scenario==6 and offset==2:
                    status="send_failed"
                cursor = connection.execute("INSERT INTO campaign_recipients(campaign_id,supplier_id,email,status,sent_at,last_response_at,completed_at,notes) VALUES(?,?,?,?,?,?,?,?)",
                    (campaign_id,supplier_id,address,status,None if state=="draft" else stamp(created+timedelta(hours=1)),
                    stamp(created+timedelta(days=2)) if status in {"files_received","declined","reply_without_files"} else None,
                    stamp(created+timedelta(days=2)) if status in {"files_received","declined"} else None,
                    "Демонстрация ошибки отправки" if status=="send_failed" else ""))
                recipient_id=cursor.lastrowid
                if state=="draft":
                    continue
                parent=outgoing(recipient_id,address,0,body,created+timedelta(hours=1),kind="campaign",
                                delivery="failed" if status=="send_failed" else "sent")
                if status in {"sent","send_failed"}:
                    continue
                if status=="declined":
                    incoming(recipient_id,address,1,"Добрый день! Спасибо за запрос. К сожалению, нужных позиций сейчас нет в наличии. "
                        "Поставку в указанный срок подтвердить не можем. Вернёмся к вам при поступлении.\n\nС уважением, отдел продаж.",
                        created+timedelta(hours=4),parent)
                    continue
                if scenario==2:
                    first="Здравствуйте, Анна! Для продления Google Workspace уточните количество пользователей и срок лицензий. Нужна ли помощь с переносом почты Gmail?"
                    answer="Добрый день! Нужно 25 пользователей на 12 месяцев. Почта уже используется, перенос данных не нужен. Пришлите отдельные строки по лицензиям и сопровождению."
                    offer="Анна, направляю тестовое предложение по Google Workspace. Лицензии на год, сопровождение выделено отдельно. Существующие письма Gmail сохраняются. Счёт и условия во вложении."
                else:
                    first="Анна, добрый день! Получили заявку. Уточните, пожалуйста, адрес доставки и допускается ли частичная поставка. Расчёт подготовим сегодня до 16:00."
                    answer="Добрый день! Адрес доставки — наш склад в Москве. Частичная поставка допустима двумя партиями. В предложении укажите стоимость доставки и срок готовности каждой партии."
                    offer=f"Добрый день! Направляю предложение: {product}. Первая партия доступна со склада, остаток — через 5 рабочих дней. Оплата 30/70, цены с НДС. Счёт и условия во вложении."
                parent,in_id=incoming(recipient_id,address,1,first,created+timedelta(hours=4),parent)
                parent=outgoing(recipient_id,address,2,answer+"\n\n> "+first,created+timedelta(hours=6),parent,in_id)
                if status=="reply_without_files":
                    incoming(recipient_id,address,3,"Спасибо за уточнение. Расчёт ещё согласовываем. Финальное предложение со счётом направим завтра утром.",created+timedelta(days=1,hours=1),parent)
                    continue
                parent,in_id=incoming(recipient_id,address,3,offer+"\n\n> "+answer,created+timedelta(days=1,hours=2),parent,attach=True)
                parent=outgoing(recipient_id,address,4,"Спасибо, предложение получили. Подтвердите, пожалуйста, срок действия цены и возможность оплаты после согласования договора.\n\n> "+offer,
                                created+timedelta(days=1,hours=5),parent,in_id)
                incoming(recipient_id,address,5,"Подтверждаем: предложение действует 7 календарных дней. Договор готовы согласовать по вашей форме. Ответственный менеджер будет на связи до завершения поставки.",
                         created+timedelta(days=2,hours=1),parent)

            if state!="draft":
                incoming(None,f"documents{scenario}@gmail.example.test",8,"Добрый день! Пересылаю коммерческое предложение по вашему обращению. "
                    "Подскажите, к какой заявке его отнести?\n\nЭто тестовое непривязанное письмо для ручной обработки.",
                    ANCHOR-timedelta(hours=scenario+1),None,attach=scenario%2==0,review=True)
            if state in {"ready","closed"}:
                summary=root / "summary" / "Сводная_демо.xlsx"
                _sheet(summary,"Сравнение тестовых предложений",["№","Поставщик","Товар","Цена, ₽","Статус"],
                    [[n+1,VENDORS[index][0],product,1600+scenario*850+n*65,"Получено"] for n,index in enumerate(indexes[:-1])])
                summaries.append((campaign_id,summary,created))
            connection.execute("INSERT INTO audit_log(created_at,level,event_type,campaign_id,details) VALUES(?,?,?,?,?)",
                (stamp(created),"info","demo_data_created",campaign_id,json.dumps({"fixture":SEED},ensure_ascii=False)))
        connection.execute("INSERT INTO settings(key,value) VALUES(?,?)",(SEED,"done"))

    # Completed demo jobs cannot be picked up by workers or invoke paid services.
    for campaign_id,path,created in summaries:
        file_id=runtime.register_file(user["id"],path)
        job_id=hashlib.sha256(f"{SEED}:{user['id']}:{campaign_id}".encode()).hexdigest()[:32]
        with store.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO jobs(id,user_id,kind,payload,state,created,updated,message,result) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id,user["id"],"summary","{}","done",created.timestamp(),created.timestamp()+3600,
                 "Тестовая сводная готова. Данные демонстрационные.",json.dumps({"file_id":file_id,"campaign_id":campaign_id})))
    return counts
