"""Evidence-bounded thread classification; Gmail message bodies remain private."""
from __future__ import annotations
import base64
import datetime as dt
import email.utils
import email.parser
import email.policy
import html
from html.parser import HTMLParser
import re
import unicodedata

UTC = dt.timezone.utc


class BodyError(ValueError):
    pass


def raw_payload(encoded):
    if not isinstance(encoded, str): raise BodyError('Raw message was not text')
    truncated = re.search(r'…\d+ chars truncated…', encoded)
    if truncated:
        # Codex JSONL caps very large strings. A large binary attachment may be
        # omitted after the complete primary MIME text. Decode only the intact
        # prefix and require that the cut falls inside a non-text attachment.
        prefix = encoded[:truncated.start()]
        if not re.fullmatch(r'[A-Za-z0-9_\-]+', prefix):
            raise BodyError('Truncated raw message has an invalid encoded prefix')
        encoded = prefix[:len(prefix) // 4 * 4]
    # Connector versions can return either encoded bytes or already-decoded
    # RFC2822 text. Accept the latter only when it has actual mail headers.
    if re.match(r'^(?:Received|Delivered-To|MIME-Version|From|Date|Return-Path|Subject):', encoded, re.I):
        data = encoded.encode('utf-8')
    else:
        try: data = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
        except (ValueError, UnicodeError) as exc: raise BodyError('Raw message is neither encoded RFC2822 nor mail-header text') from exc
    message = email.parser.BytesParser(policy=email.policy.default).parsebytes(data)
    if truncated:
        leaves = [part for part in message.walk() if not part.is_multipart()]
        primary = [part for part in leaves if part.get_content_maintype() == 'text' and not part.get_filename()]
        last = leaves[-1] if leaves else None
        if (not primary or not last or last.get_content_maintype() == 'text' or
                not (last.get_filename() or last.get_content_disposition() == 'attachment') or
                any(part.defects for part in primary)):
            raise BodyError('Raw log limit reached before complete primary message text')
    def convert(part):
        payload = {'mime_type': part.get_content_type(), 'headers': [{'name': k, 'value': str(v)} for k, v in part.items()],
                   'filename': part.get_filename() or '', 'body': {}, 'parts': []}
        if part.is_multipart(): payload['parts'] = [convert(c) for c in part.iter_parts()]
        elif part.get_content_maintype() == 'text':
            raw = part.get_payload(decode=True) or b''
            payload['body'] = {'base64_url_content': base64.urlsafe_b64encode(raw).decode(), 'size': len(raw)}
        return payload
    return convert(message)


def headers(part):
    return {str(x.get('name', '')).lower(): str(x.get('value', '')) for x in part.get('headers') or []}


class TextHTML(HTMLParser):
    def __init__(self, keep_quotes=False):
        super().__init__(); self.out = []; self.skip = 0; self.keep_quotes = keep_quotes
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style') or (not self.keep_quotes and (tag == 'blockquote' or 'gmail_quote' in attrs.get('class', ''))):
            self.skip += 1
        if tag in ('br', 'p', 'div', 'tr', 'li'):
            self.out.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'blockquote') and self.skip:
            self.skip -= 1
        if tag in ('p', 'div', 'tr'):
            self.out.append('\n')
    def handle_data(self, value):
        if not self.skip: self.out.append(value)


def decode_part(part):
    body = part.get('body') or {}
    raw = body.get('base64_url_content', body.get('data'))
    if raw:
        try: data = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
        except Exception as exc: raise BodyError('Invalid MIME body encoding') from exc
        content_type = headers(part).get('content-type', '')
        match = re.search(r'charset\s*=\s*["\']?([^;"\'\s]+)', content_type, re.I)
        charset = match.group(1).lower() if match else 'utf-8'
        charset = {'ks_c_5601-1987': 'cp949', 'ks_c_5601': 'cp949', 'euc-kr': 'cp949'}.get(charset, charset)
        for encoding in dict.fromkeys((charset, 'utf-8', 'cp949')):
            try: return data.decode(encoding)
            except (LookupError, UnicodeError): continue
        raise BodyError('MIME charset could not be decoded')
    if isinstance(body.get('content'), str):
        if '\ufffd' in body['content']:
            raise BodyError('Decoded body contains replacement characters')
        return body['content']
    if body.get('size') and body.get('attachment_id'):
        raise BodyError('Text body requires a separate attachment read')
    return ''


def top_body(payload, include_quoted=False):
    plain, rich = [], []
    def walk(part):
        mime = part.get('mime_type', part.get('mimeType', '')).lower()
        disposition = headers(part).get('content-disposition', '')
        if disposition.lower().startswith('attachment') or part.get('filename'):
            return
        if mime == 'text/plain':
            decoded = decode_part(part)
            # Some recruiter clients send an empty plain alternative alongside
            # a complete HTML body. Its presence must not hide that body.
            if decoded.strip(): plain.append(decoded)
        elif mime == 'text/html': rich.append(decode_part(part))
        for child in part.get('parts') or []: walk(child)
    walk(payload)
    if plain:
        text = '\n'.join(plain)
    else:
        parser = TextHTML(keep_quotes=include_quoted); parser.feed('\n'.join(rich)); text = ''.join(parser.out)
    # Only the author's new text can establish their own decision.
    lines = []
    for line in html.unescape(text).splitlines():
        if not include_quoted:
            if re.match(r'^\s*(?:>+|On .{5,150}wrote:|[-_]{3,}\s*(?:Original Message|원본 메시지|Forwarded)|보낸\s*사람\s*:|From\s*:)', line, re.I): break
            if re.match(r'^\s*\d{4}년\s*\d+월\s*\d+일.*(?:작성|wrote)', line): break
        lines.append(line.rstrip())
    text = unicodedata.normalize('NFKC', '\n'.join(lines)).strip()
    if not text:
        raise BodyError('Message has no readable author body')
    return text


def timestamp(message):
    value = message.get('internal_date', message.get('internalDate'))
    try: return dt.datetime.fromtimestamp(int(value) / 1000, UTC).isoformat().replace('+00:00', 'Z')
    except (ValueError, TypeError, OSError): pass
    try:
        date = email.utils.parsedate_to_datetime(headers(message.get('payload', {})).get('date', ''))
        return date.astimezone(UTC).isoformat().replace('+00:00', 'Z')
    except Exception as exc: raise BodyError('Message has no valid sent/received time') from exc


PROPOSAL = r'포지션\s*(?:제안|소개|안내)|(?:채용|입사|이직)\s*제안|position\s+(?:proposal|opportunity)|career\s+opportunity'
DECLINE = r'고사(?:하|드|를)|지원(?:을|은|를)?\s*(?:하지\s*않|안\s*하|안\s*할|않겠|어려|어렵|포기)|진행(?:을|은)?\s*(?:하지\s*않|어려|어렵|않겠)|정중히\s*거절|decline\s+(?:the|this|your)|not\s+(?:be\s+)?(?:interested|proceeding)|won.t\s+(?:apply|proceed)'
WITHDRAW = r'(?:지원|전형|면접)\s*(?:을|를|은)?\s*(?:철회|중단|취소)|withdraw\s+(?:my\s+)?application'
HOLD = r'(?:검토|고려)(?:해|하|하여|하고)\s*(?:보|있|후)|(?:추후|나중에|다음에).{0,15}(?:검토|연락)|보류|시간.{0,10}(?:필요|주시면)|consider\s+(?:it|the|this)|review\s+(?:it|the|this)'
APPLY = r'지원(?:을|하고자)?\s*(?:하겠|합니다|할게|희망|진행)|지원\s*의사(?:가|를|는)?\s*(?:있|밝히|전달|드립니다)|(?:이력서|지원서|서류|resume|cv).{0,35}(?:첨부(?:합니다|드립니다|하였|했)|송부(?:합니다|드립니다|했)|전달(?:합니다|드립니다|했)|보냅니다|보내드립니다|attached|attach)|(?:apply|applying)\s+(?:for|to)|please\s+(?:submit|proceed)'
CONDITIONAL_APPLY = r'지원\s*의사.{0,15}(?:없|않)|아직.{0,30}(?:지원|이력서).{0,20}(?:않|없|못)|(?:조건|처우|연봉).{0,30}(?:확인한\s*후|확인\s*후|맞으면|충족|괜찮으면)|(?:검토|확인).{0,8}(?:후에|후|뒤).{0,25}(?:지원|이력서)|if.{0,50}(?:apply|resume|proceed)'
RECEIPT = r'지원(?:서|이)?\s*(?:정상적으로\s*)?(?:접수|완료)|지원해\s*주셔서|서류(?:가|를)?\s*(?:접수|전달|추천).{0,15}(?:완료|드렸|했습니다)|application\s+(?:received|submitted)|thank\s+you\s+for\s+applying'
REJECT = r'불합격|(?:함께|다음\s*단계).{0,25}(?:어렵|어려|못하)|(?:전형|지원).{0,25}(?:탈락|아쉽)|not\s+(?:be\s+)?(?:moving|proceeding)\s+(?:forward|further|with\s+(?:your|the)\s+(?:application|candidacy))|unfortunately.{0,60}(?:application|position)|(?:decided|deciding|chosen)\s+to\s+(?:move\s+forward|proceed)\s+with\s+other\s+candidates|(?:다른|적합한)\s*후보.{0,20}(?:진행|채용)|(?:요건|기준).{0,20}차이가\s*(?:있다는|있어|있다고)|모시기.{0,12}(?:어려|어렵)|모시지\s*못하|탈락\s*사유(?:였|입|가)|잘\s*안\s*되(?:셨어|셨)|(?:긍정적인|좋은|합격)\s*(?:소식|결과).{0,35}(?:이어지지\s*못|전해\s*드리지\s*못|전하지\s*못)|인연을\s*이어가지\s*못'
INTERVIEW = r'(?:면접|인터뷰|과제|인적성).{0,25}(?:안내|일정|요청|진행|참석|전형)|interview\s+(?:invitation|schedule|with)|assessment\s+(?:invitation|request)'
OFFER = r'(?:최종\s*합격|오퍼\s*(?:레터|안내|제안)|처우\s*(?:협의|제안))|offer\s+letter|pleased\s+to\s+offer'
HIRED = r'입사(?:일|\s*확정|를\s*환영)|온보딩\s*안내|welcome\s+aboard|start\s+date\s+(?:is|confirmed)'
RECRUITMENT = r'채용|구직|이직|입사|헤드헌|이력서|지원서|지원\s*(?:공고|분야)|지원한\s*공고|지원(?:하신|하셨|해\s*주)|서류\s*전형|전형\s*(?:결과|안내|일정)|면접|\b(?:recruitment|recruiter|hiring|candidate|candidates|candidacy|resume|interview)\b|\b(?:job|career)\s+(?:application|opportunity)|\bapplication\s+(?:decision|result|received|submitted)|\byour\s+application\b|thank\s+you\s+for\s+applying'
RESULT_SUBJECT = r'(?:서류|전형|지원|채용).{0,25}(?:결과|불합격|탈락)|불합격\s*안내|\bapplication\s+(?:decision|result)|\b(?:interview|recruitment)\s+(?:decision|result)'
NEWSLETTER_SUBJECT = r'job\s*alert|채용\s*알림|뉴스레터|맞춤\s*공고|추천\s*공고|newsletter'


def decision_text(text):
    """Remove general hiring-process descriptions before judging a decision."""
    return '\n'.join(line for line in text.splitlines() if not re.search(
        r'(?:채용|전형)\s*(?:절차|프로세스)|(?:서류|면접|인터뷰|처우).*(?:[-→>]).*(?:면접|인터뷰|합격|처우)|불합격자(?:에게|는|에\s*한해)|합격자(?:에게|에\s*한해)', line))


def identity(subject, body):
    company, role = '', ''
    brackets = re.findall(r'\[([^]\n]{2,80})\]', subject)
    generic = re.compile(r'채용|헤드헌|제안|안내|지원|리멤버|원티드|사람인|잡코리아|포지션', re.I)
    candidates = [c.strip() for c in brackets if not generic.search(c)]
    def company_name(value):
        value = re.sub(r'^(?:\(주\)|㈜|주식회사)\s*', '', value.strip()).strip()
        return re.sub(r'\s*(?:주식회사|\(주\)|㈜)$', '', value).strip()
    company_fields = re.findall(r'(?:회사명|기업명|고객사|채용회사)\s*[:：]\s*([^\n]{2,80})', body)
    company_fields += re.findall(r'(?:^|\n)[ \t]*(?:회사명|기업명|고객사|채용회사)[ \t]*\n+\s*([^\n]{2,80})', body)
    company_fields += re.findall(r'\[(?:회사정보|회사명|고객사|채용회사)\]\s*([^\n]{2,100})', body)
    explicit_companies = list(dict.fromkeys(company_name(re.split(r'\s*https?://', m)[0]) for m in company_fields))
    if len(explicit_companies) == 1:
        company = explicit_companies[0]
    elif not explicit_companies and len(candidates) == 1:
        company = company_name(candidates[0])
    # A recruiter can use its own bracket while naming the actual employer in
    # both the subject and the result notice. Require both, not company-only
    # inference from a signature or an incidental mention.
    result_subject = re.sub(r'^(?:(?:Re|Fw|Fwd):\s*|\[[^]]+\]\s*)+', '', subject, flags=re.I)
    result_company = re.match(r'^(.{2,80}?)\s+(?:서류\s*)?전형\s*결과\s*안내', result_subject)
    agency_company = re.search(r'서치|헤드헌|커리어|search|career', company, re.I)
    if not explicit_companies and result_company and (not company or agency_company):
        named_company = company_name(result_company.group(1))
        process_label = re.search(r'지원(?:서)?|이력서|서류|면접|전형|채용|리뷰|심사|검토', named_company)
        if not process_label and re.search(re.escape(named_company) + r'\s+(?:서류\s*)?전형\s*결과\s*안내', body):
            company = named_company
    combined_bracket = re.match(r'^(.+?)\s+[-|:]\s+((?:재무|회계|경영지원|Finance|Accounting|자금|CFO).+)$', company, re.I)
    bracket_role = ''
    if combined_bracket: company, bracket_role = company_name(combined_bracket.group(1)), combined_bracket.group(2).strip()
    if company:
        bracket = next((c for c in brackets if company_name(c) == company), '')
        after = subject.split('[' + bracket + ']', 1)[-1] if bracket else ''
        after = re.sub(r'^(?:\s*[-:|])?\s*', '', after)
        role = re.split(r'\s*(?:포지션|채용\s*(?:제안|안내)|제안드|지원\s*(?:결과|접수)|서류\s*(?:결과|전형|접수)|(?:인터뷰|면접)\s*결과|직무\s*지원)', after)[0].strip(' -:|')
    explicit_roles = list(dict.fromkeys(m.strip() for m in re.findall(r'(?:포지션|채용직무|모집분야|지원분야|직무)\s*[:：]\s*([^\n]{2,100})', body)))
    if len(explicit_roles) == 1: role = explicit_roles[0]
    elif len(explicit_roles) > 1: role = ''
    elif bracket_role: role = bracket_role
    elif company:
        named_roles = list(dict.fromkeys(m.strip() for m in re.findall(
            re.escape(company) + r'\s+((?:재무|회계|경영지원|Finance|Accounting|자금|CFO)[^\n]{0,70}?)\s+포지션', body, re.I)))
        if len(named_roles) == 1: role = named_roles[0]
    # Receipt templates state company/role explicitly even when the bracket is
    # the job-board name and the subject is addressed to the candidate.
    wanted_subject = re.sub(r'^\s*\[[^]]+\]\s*', '', subject)
    wanted_subject = re.sub(r'^.*?님[!,:]\s*', '', wanted_subject)
    receipt = re.search(r'^(.{2,80}?)의\s+(.{2,120}?)에\s+지원(?:이)?\s*(?:정상적으로\s*)?완료', wanted_subject)
    if receipt:
        company, role = company_name(receipt.group(1)), receipt.group(2).strip()
    greeting = re.search(r'님의?\s+([^\n]{2,120}?)\s+지원이\s*(?:정상적으로\s*)?완료', body)
    if greeting: role = greeting.group(1).strip()
    posting = re.search(r'지원(?:한)?\s*공고\s*\n+\s*([^\n]{2,120})', body)
    if posting and not explicit_roles:
        role = posting.group(1).strip()
        if company: role = re.sub(r'^\[' + re.escape(company) + r'\]\s*', '', role)
    applied_role = re.search(r'지원(?:해\s*주셨던|하셨던|하신)\s+([^\n]{2,120}?)\s*건에\s*대해', body)
    if applied_role and not explicit_roles:
        role = applied_role.group(1).strip()
    role_from_result = re.search(r'(?:이번\s+)?' + re.escape(company) + r'의\s+([^\n]{2,120}?)\s+모집에', body) if company else None
    if role_from_result: role = role_from_result.group(1).strip()
    if not company:
        proposal_subject = re.sub(r'^(?:Re:|Fw:|Fwd:)\s*', '', subject, flags=re.I)
        proposal_subject = re.sub(r'^.*?님[,!：:]\s*', '', proposal_subject)
        proposal = re.search(r'^([^\[\]\n]{2,80}?)\s+((?:재무|회계|경영지원|Finance|Accounting|자금|CFO)[^\n]{0,80}?)\s+포지션\s*(?:제안|소개|안내)', proposal_subject, re.I)
        if proposal: company, role = company_name(proposal.group(1)), proposal.group(2).strip()
    if re.search(r'님|지원이|정상적으로|완료되|안내\s*드|채용\s*담당|(?:서류|지원서?|이력서)\s*(?:전형|접수|심사)|(?:접수|지원|채용)\s*(?:완료|결과|안내)|결과\s*안내', role) or re.fullmatch(r'채용|모집|포지션|지원|안내', role):
        role = ''
    # Subject is retained as a human-review title when identity is not explicit.
    return company, role[:150]


def evidence(text, pattern):
    match = re.search(pattern, text, re.I | re.S)
    if not match: return text[:500]
    start = max(text.rfind('\n', 0, match.start()), text.rfind('. ', 0, match.start())) + 1
    end = text.find('\n', match.end())
    if end < 0: end = min(len(text), match.end() + 120)
    value = text[start:end].strip()[:800]
    value = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[이메일]', value)
    return re.sub(r'(?<!\d)01[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)', '[연락처]', value)


def classify_thread(thread):
    tid = thread.get('id') or thread.get('thread_id')
    if not tid: raise BodyError('Thread ID missing')
    messages = []
    for message in thread.get('messages') or []:
        labels = set(message.get('label_ids', message.get('labelIds', [])) or [])
        if labels & {'DRAFT', 'SPAM', 'TRASH'}: continue
        payload = message.get('payload') or {}
        h = headers(payload)
        subject = h.get('subject', '')
        if re.search(NEWSLETTER_SUBJECT, subject, re.I):
            continue
        forwarded_only = False
        try: body = top_body(payload)
        except BodyError as exc:
            if str(exc) == 'Message has no readable author body' and re.match(r'^\s*(?:FW|FWD|전달)\s*[:：]', subject, re.I):
                body = top_body(payload, include_quoted=True)
                forwarded_only = True
            # Only relevant unreadable messages block completeness.
            elif re.search(PROPOSAL + '|' + RECEIPT + '|면접|지원|채용', subject, re.I): raise
            else: continue
        # ATS result notices can carry unsubscribe/list headers too. Only a
        # concrete result notice may bypass those bulk-mail markers; generic
        # newsletters and descriptions of rejection policy remain excluded.
        if 'list-unsubscribe' in h or 'list-id' in h:
            if not (re.search(RESULT_SUBJECT, subject, re.I) and
                    re.search(REJECT, decision_text(body), re.I | re.S)):
                continue
        messages.append({'id': message.get('id'), 'at': timestamp(message), 'sent': 'SENT' in labels,
                         'subject': subject, 'body': body, 'forwarded_only': forwarded_only})
    messages.sort(key=lambda m: (m['at'], m['id'] or ''))
    if not messages: return []
    subject = messages[0]['subject']
    context = '\n'.join(m['body'] for m in messages)
    company, role = identity(subject, context)
    all_subjects = '\n'.join(m['subject'] for m in messages)
    recruitment = bool(re.search(RECRUITMENT + '|' + PROPOSAL + '|' + RECEIPT,
                                 all_subjects + '\n' + context, re.I | re.S))
    relevant = bool(re.search(PROPOSAL, subject + '\n' + context, re.I) or
                    (recruitment and re.search(RECEIPT + '|' + INTERVIEW + '|' + OFFER + '|' + REJECT,
                                               all_subjects + '\n' + context, re.I | re.S)) or
                    any(m['sent'] and re.search(APPLY, m['body'], re.I) for m in messages) or
                    any(m['forwarded_only'] and re.search(r'지원|채용|전형|면접', m['subject']) for m in messages))
    if not relevant: return []
    source = 'headhunter_mail' if re.search(r'헤드헌|서치펌|고객사|포지션\s*제안', subject + '\n' + context, re.I) else 'company_mail'
    urls = re.findall(r'https?://[^\s<>"\)]+', context)
    job_urls = [u for u in urls if re.search(r'(?:wanted\.co\.kr/wd/|rememberapp\.co\.kr/job|saramin\.co\.kr/.+rec_idx=|jobkorea\.co\.kr/.+GI_Read/|linkedin\.com/jobs/view/)', u, re.I)]
    result, current, application = [], None, False
    for m in messages:
        text, kind, pattern = m['body'], None, None
        uncertain_result = False
        # Repeated/forwarded JDs can include a generic hiring process even in a
        # reply. Process outlines never establish this person's current stage.
        status_text = decision_text(text)
        if not m['sent'] and re.search(RESULT_SUBJECT, m['subject'], re.I):
            status_text = m['subject'] + '\n' + status_text
        if m['sent']:
            rules = [('withdrawn', WITHDRAW), ('declined', DECLINE), ('applied', APPLY), ('pending', HOLD)]
        else:
            rules = [('rejected', REJECT), ('hired', HIRED), ('offer', OFFER), ('interview', INTERVIEW), ('applied', RECEIPT)]
        proposal_message = not m['sent'] and current is None and re.search(PROPOSAL, m['subject'] + '\n' + text, re.I)
        personal_result = re.search(r'지원하신.{0,100}결과|(?:이력서|서류).{0,25}검토(?:한)?\s*결과|탈락\s*사유(?:였|입|가)|(?:요건|기준).{0,20}차이가\s*(?:있다는|있어|있다고)', status_text, re.I | re.S)
        # A reply may retain the original proposal's subject. Its explicit
        # negative result must win, but a new JD describing its future hiring
        # policy is still a proposal.
        if (recruitment and re.search(REJECT, status_text, re.I | re.S) and
                (re.search(RESULT_SUBJECT, m['subject'], re.I) or not re.search(PROPOSAL, text, re.I))):
            personal_result = True
        if personal_result: proposal_message = False
        if proposal_message:
            # A proposal commonly describes future interviews/rejection policy.
            # These are not evidence that this person has entered the process.
            kind, pattern, rules = 'proposal_unanswered', PROPOSAL, []
        for possible, regex in rules:
            if possible == 'applied' and m['sent'] and re.search(CONDITIONAL_APPLY, status_text, re.I | re.S):
                continue
            if re.search(regex, status_text, re.I | re.S): kind, pattern = possible, regex; break
        if not m['sent'] and not kind and current is None and re.search(PROPOSAL, m['subject'] + '\n' + text, re.I):
            kind, pattern = 'proposal_unanswered', PROPOSAL
        if m['sent'] and not kind and current == 'proposal_unanswered':
            kind, pattern = 'pending', '.*'
        if not m['sent'] and not kind and current in ('applied', 'responded'):
            kind, pattern = 'responded', '.*'
        if m['forwarded_only'] and not kind: kind, pattern = 'pending', '.*'
        if kind == 'applied' and not m['sent'] and re.search(r'(?:서류|전형|지원|application).{0,30}(?:결과|결정|result|decision)', m['subject'], re.I):
            if not re.search(r'접수.{0,15}완료|지원이\s*(?:정상적으로\s*)?완료|application\s+(?:received|submitted)', status_text, re.I):
                uncertain_result = True
        if not kind: continue
        if kind == 'declined' and application: kind = 'withdrawn'
        if kind == 'pending' and application: continue
        if kind in ('applied', 'responded', 'interview', 'offer', 'hired', 'rejected'): application = True
        current = kind
        event = {'event_id': 'gmail:' + m['id'] + ':' + kind, 'thread_id': tid,
                 'message_id': m['id'], 'event_at': m['at'], 'company': company or '회사 확인 필요',
                 'role': role or re.sub(r'^(?:Re:|Fw:|Fwd:)\s*', '', subject, flags=re.I)[:150],
                 'kind': kind, 'evidence_text': ('전달된 메일 본문 (확인 필요): ' if m['forwarded_only'] else '') + evidence(status_text or text, pattern),
                 'mail_url': 'https://mail.google.com/mail/#all/' + tid,
                 'source_name': source, 'job_url': job_urls[0] if len(set(job_urls)) == 1 else '',
                 'needs_review': not bool(company and role) or m['forwarded_only'] or uncertain_result, 'apply': False,
                 'reason': ('전달받은 메일 — 상태 확인 필요' if m['forwarded_only'] else
                            '제안 메일 미회신' if kind == 'proposal_unanswered' else
                            '연봉 조건 불일치' if kind in ('declined', 'withdrawn') and re.search(r'연봉|급여|salary|compensation', text, re.I) else
                            '지원철회' if kind == 'withdrawn' else
                            ('지원 서류 전달' if re.search(r'이력서|지원서|서류|resume|cv', text, re.I) else '지원 의사 전달') if kind == 'applied' and m['sent'] else
                            '지원 접수 확인' if kind == 'applied' else
                            '메일에서 확인한 지원 거절' if kind == 'declined' else '메일에서 확인한 진행 상태')}
        result.append(event)
    if result: result[-1]['apply'] = True
    return result
