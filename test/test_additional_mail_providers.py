from __future__ import annotations

from unittest import TestCase, mock

from services.register import mail_provider


class FakeResponse:
    def __init__(self, payload, status_code=200, text=None, json_error=False):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload) if text is None else text
        self.headers = {"content-type": "application/json"}
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise ValueError("not JSON")
        return self.payload


class FakeCookies:
    def __init__(self):
        self.values = {"PHPSESSID": "session-1", "SUBSCR": "subscriber-1"}

    def set(self, key, value):
        self.values[str(key)] = str(value)

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = FakeCookies()
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        return response if isinstance(response, FakeResponse) else FakeResponse(response)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def close(self):
        pass


CONF = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}


class AdditionalMailProviderTests(TestCase):
    def test_mail_tm_and_mail_gw_use_api_platform_lifecycle(self):
        for cls, base in ((mail_provider.MailTmProvider, "https://api.mail.tm"), (mail_provider.MailGwProvider, "https://api.mail.gw")):
            session = FakeSession([
                {"hydra:member": [{"domain": "example.test", "isActive": True}]},
                {"id": "account-1", "address": "user@example.test"},
                {"token": "token-1"},
                {"hydra:member": [{"id": "message-1", "subject": "code", "createdAt": "2026-06-10T12:00:00Z", "from": {"address": "sender@example.test"}}]},
                {"id": "message-1", "subject": "code", "text": "123456", "html": ["<b>123456</b>"], "from": {"address": "sender@example.test"}},
            ])
            with mock.patch.object(mail_provider, "_create_session", return_value=session):
                provider = cls({"provider_ref": f"{cls.name}#1"}, CONF)
                mailbox = provider.create_mailbox("user")
                message = provider.fetch_latest_message(mailbox)
                provider.close()
            self.assertEqual(mailbox["address"], "user@example.test")
            self.assertEqual(message["text_content"], "123456")
            self.assertEqual(message["html_content"], "<b>123456</b>")
            self.assertEqual(session.calls[0]["url"], f"{base}/domains")

    def test_dropmail_graphql_session_and_mail(self):
        session = FakeSession([
            {"data": {"introduceSession": {"id": "session-1", "expiresAt": "2026-06-10T12:00:00Z", "addresses": [{"address": "abc@dropmail.me", "restoreKey": "restore"}]}}},
            {"data": {"session": {"mails": [{"id": "mail-1", "toAddr": "abc@dropmail.me", "fromAddr": "sender@example.test", "headerSubject": "Code", "text": "123456", "receivedAt": "2026-06-10T12:01:00Z"}]}}},
        ])
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.DropMailProvider({"provider_ref": "dropmail#1", "api_key": "af_test"}, CONF)
            mailbox = provider.create_mailbox()
            message = provider.fetch_latest_message(mailbox)
        self.assertEqual(mailbox["session_id"], "session-1")
        self.assertEqual(message["message_id"], "mail-1")
        self.assertEqual(message["text_content"], "123456")

    def test_guerrilla_mail_persists_cookie_and_fetches_detail(self):
        session = FakeSession([
            {"email_addr": "abc@guerrillamailblock.com", "email_timestamp": 1},
            {"list": [{"mail_id": "42", "mail_subject": "Code", "mail_from": "sender@example.test", "mail_timestamp": 2}]},
            {"mail_subject": "Code", "mail_from": "sender@example.test", "mail_body_plain": "123456", "mail_timestamp": 2},
        ])
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.GuerrillaMailProvider({"provider_ref": "guerrilla_mail#1"}, CONF)
            mailbox = provider.create_mailbox()
            message = provider.fetch_latest_message(mailbox)
        self.assertEqual(mailbox["php sessid"] if "php sessid" in mailbox else mailbox["phpsessid"], "session-1")
        self.assertEqual(message["message_id"], "42")
        self.assertEqual(session.calls[1]["params"]["SUBSCR"], "subscriber-1")

    def test_maildrop_and_catchmail_read_listing_then_detail(self):
        maildrop_session = FakeSession([
            FakeResponse({"data": {"inbox": [{"id": "m1", "headerfrom": "a@example.test", "subject": "Code", "date": "2026-06-10T12:00:00Z"}]}}),
            FakeResponse({"data": {"message": {"id": "m1", "headerfrom": "a@example.test", "subject": "Code", "date": "2026-06-10T12:00:00Z", "data": "123456", "html": "<b>123456</b>"}}}),
        ])
        with mock.patch.object(mail_provider, "_create_session", return_value=maildrop_session):
            provider = mail_provider.MaildropProvider({"provider_ref": "maildrop#1"}, CONF)
            message = provider.fetch_latest_message(provider.create_mailbox("box"))
        self.assertEqual(message["text_content"], "123456")
        self.assertEqual(maildrop_session.calls[0]["json"]["variables"], {"mailbox": "box"})
        self.assertEqual(maildrop_session.calls[1]["json"]["variables"]["mailbox"], "box")

        catchmail_session = FakeSession([
            {"messages": [{"id": "c1", "mailbox": "box@catchmail.io", "subject": "Code", "date": "2026-06-10T12:00:00Z"}]},
            {"id": "c1", "mailbox": "box@catchmail.io", "from": "a@example.test", "subject": "Code", "date": "2026-06-10T12:00:00Z", "body": {"text": "123456", "html": "<b>123456</b>"}},
        ])
        with mock.patch.object(mail_provider, "_create_session", return_value=catchmail_session):
            provider = mail_provider.CatchmailProvider({"provider_ref": "catchmail#1"}, CONF)
            message = provider.fetch_latest_message(provider.create_mailbox("box"))
        self.assertEqual(message["html_content"], "<b>123456</b>")

    def test_json_inbox_providers_normalize_messages(self):
        cases = [
            (mail_provider.CleanTempMailProvider, {"api_key": "clean-test-key"}, [{"email": "box@clean.test"}, {"emails": [{"id": "1", "to": "box@clean.test", "subject": "Code", "content": "123456"}]}, {"id": "1", "subject": "Code", "content": "123456"}]),
            (mail_provider.DustMailProvider, {"api_key": "dm-test"}, [{"id": "inbox-1", "address": "box@dust.test"}, {"messages": [{"id": "1", "to": "box@dust.test", "subject": "Code", "text": "123456"}]}]),
            (mail_provider.MailiskProvider, {"api_key": "mk-test", "namespace": "ns"}, [{"data": [{"id": "1", "to": [{"address": "box@ns.mailisk.net"}], "subject": "Code", "text": "123456"}]}]),
            (mail_provider.MailsacProvider, {}, [
                FakeResponse([{"_id": "1", "to": [{"address": "box@mailsac.com"}], "subject": "Code", "received": "2026-06-10T12:00:00Z"}]),
                FakeResponse("123456", text="123456", json_error=True),
                FakeResponse("<b>123456</b>", text="<b>123456</b>", json_error=True),
            ]),
        ]
        for cls, entry, responses in cases:
            if cls is mail_provider.MailsacProvider:
                responses = [item if isinstance(item, FakeResponse) else FakeResponse(item) for item in responses]
            with mock.patch.object(mail_provider, "_create_session", return_value=FakeSession(responses)):
                provider = cls({"provider_ref": f"{cls.name}#1", **entry}, CONF)
                mailbox = provider.create_mailbox("box")
                message = provider.fetch_latest_message(mailbox)
                provider.close()
            self.assertIsNotNone(message, cls.name)
            if cls is mail_provider.MailsacProvider:
                self.assertEqual(message["text_content"], "123456")
                self.assertEqual(message["html_content"], "<b>123456</b>")

    def test_cleantempmail_accepts_plain_text_address(self):
        session = FakeSession([FakeResponse("box@clean.test", text="box@clean.test", json_error=True)])
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.CleanTempMailProvider({"provider_ref": "cleantempmail#1", "api_key": "clean-test-key"}, CONF)
            mailbox = provider.create_mailbox()
        self.assertEqual(mailbox["address"], "box@clean.test")

    def test_dustmail_accepts_list_wrapped_inbox_response(self):
        session = FakeSession([
            FakeResponse({"id": "inbox-1", "address": "box@dust.test"}),
            FakeResponse({"data": [{"id": "message-1", "to": "box@dust.test", "subject": "Code", "text": "123456"}]}),
        ])
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.DustMailProvider({"provider_ref": "dustmail#1", "api_key": "key"}, CONF)
            mailbox = provider.create_mailbox()
            message = provider.fetch_latest_message(mailbox)
        self.assertEqual(message["message_id"], "message-1")

    def test_namespace_and_testmail_generation(self):
        for cls, entry, suffix in (
            (mail_provider.TestmailAppProvider, {"api_key": "key", "namespace": "ns", "default_domain": "wrong.test"}, "@inbox.testmail.app"),
            (mail_provider.MailiskProvider, {"api_key": "key", "namespace": "ns", "default_domain": "wrong.test"}, "@ns.mailisk.net"),
        ):
            with mock.patch.object(mail_provider, "_create_session", return_value=FakeSession([])):
                provider = cls({"provider_ref": f"{cls.name}#1", **entry}, CONF)
                mailbox = provider.create_mailbox("box")
            self.assertTrue(mailbox["address"].endswith(suffix))

    def test_mailisk_uses_supported_limit_and_target_prefix(self):
        session = FakeSession([{"data": []}])
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.MailiskProvider({"provider_ref": "mailisk#1", "api_key": "key", "namespace": "ns"}, CONF)
            provider.fetch_latest_message(provider.create_mailbox("box"))
        self.assertEqual(session.calls[0]["params"]["limit"], 20)
        self.assertEqual(session.calls[0]["params"]["to_addr_prefix"], "box@")

    def test_factory_registers_all_new_types(self):
        names = [
            "mail_tm", "mail_gw", "dropmail", "guerrilla_mail", "maildrop", "catchmail",
            "dustmail", "cleantempmail", "testmail_app", "mailisk", "mailsac",
            "tempy_email", "qack", "smails", "agentmail", "mailslurp", "mailosaur",
        ]
        for name in names:
            config = {"providers": [{"type": name, "enable": True, **({"api_key": "account-test-key"} if name in {"agentmail", "mailslurp", "mailosaur"} else {}), **({"server_id": "server-1"} if name == "mailosaur" else {})}]}
            with mock.patch.object(mail_provider, "_create_session", return_value=FakeSession([])):
                provider = mail_provider._create_provider(config)
            self.assertEqual(provider.name, name)
            provider.close()

    def test_mailslurp_creates_inbox_and_reads_email_detail(self):
        session = FakeSession([
            {"id": "inbox-1", "emailAddress": "box@mailslurp.test"},
            {"content": [{"id": "email-1"}]},
            {"id": "email-1", "to": [{"emailAddress": "box@mailslurp.test"}], "subject": "Code", "body": "Verification code: 123456", "createdAt": "2026-06-10T12:00:00Z"},
        ])
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.MailSlurpProvider({"provider_ref": "mailslurp#1", "api_key": "key"}, CONF)
            mailbox = provider.create_mailbox("box")
            message = provider.fetch_latest_message(mailbox)
        self.assertEqual(mailbox["inbox_id"], "inbox-1")
        self.assertEqual(message["message_id"], "email-1")
        self.assertEqual(message["text_content"], "Verification code: 123456")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "key")
        self.assertIn("/inboxes/inbox-1/emails", session.calls[1]["url"])

    def test_mailosaur_lists_messages_with_basic_auth(self):
        session = FakeSession([{
            "items": [{"id": "message-1", "to": [{"email": "box@server-1.mailosaur.net"}], "subject": "Code", "text": {"body": "Verification code: 123456"}, "received": "2026-06-10T12:00:00Z"}],
        }])
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.MailosaurProvider({"provider_ref": "mailosaur#1", "api_key": "key", "server_id": "server-1"}, CONF)
            mailbox = provider.create_mailbox("box")
            message = provider.fetch_latest_message(mailbox)
        self.assertEqual(message["message_id"], "message-1")
        self.assertEqual(message["text_content"], "Verification code: 123456")
        self.assertTrue(session.calls[0]["headers"]["Authorization"].startswith("Basic "))
        self.assertEqual(session.calls[0]["params"]["server"], "server-1")

    def test_tempy_email_lifecycle_and_empty_inbox(self):
        session = FakeSession([
            {"email": "box@tempy.test", "expiresAt": "2026-06-10T13:00:00Z", "secondsRemaining": 3600},
            {"messages": []},
        ])
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.TempyEmailProvider({"provider_ref": "tempy_email#1"}, CONF)
            mailbox = provider.create_mailbox("ignored")
            message = provider.fetch_latest_message(mailbox)
            provider.close()
        self.assertEqual(mailbox["address"], "box@tempy.test")
        self.assertEqual(mailbox["seconds_remaining"], 3600)
        self.assertIsNone(message)
        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://tempy.email/api/v1/mailbox")
        self.assertEqual(session.calls[0]["json"], {})
        self.assertEqual(session.calls[1]["url"], "https://tempy.email/api/v1/mailbox/box@tempy.test/messages")

    def test_qack_reads_listing_and_details_with_target_filter(self):
        session = FakeSession([
            {"address": "box@qack.test"},
            [
                {"id": "wrong", "to": "other@qack.test", "subject": "wrong", "received_at": "2026-06-10T12:02:00Z"},
                {"id": "q1", "to": "box@qack.test", "subject": "Code", "received_at": "2026-06-10T12:01:00Z"},
            ],
            {"id": "q1", "from": "sender@example.test", "to": "box@qack.test", "subject": "Code", "body": {"text": "Verification code: 123456"}},
        ])
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.QackProvider({"provider_ref": "qack#1", "realistic": True}, CONF)
            mailbox = provider.create_mailbox()
            message = provider.fetch_latest_message(mailbox)
        self.assertEqual(message["message_id"], "q1")
        self.assertEqual(message["text_content"], "Verification code: 123456")
        self.assertEqual(session.calls[0]["json"], {"realistic": True})
        self.assertEqual(session.calls[1]["url"], "https://api.qack.dev/v1/inboxes/box@qack.test/messages")
        self.assertEqual(session.calls[2]["url"], "https://api.qack.dev/v1/inboxes/box@qack.test/messages/q1")

    def test_smails_persists_bearer_token_and_reads_detail(self):
        session = FakeSession([
            {"address": "box@smails.test", "token": "mailbox-token"},
            [{"id": "s1", "to": "box@smails.test", "from_addr": "a@example.test", "subject": "Code", "received_at": "2026-06-10T12:00:00Z"}],
            {"id": "s1", "from_addr": "a@example.test", "subject": "Code", "text": "123456", "html": "<b>123456</b>"},
        ])
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.SmailsProvider({"provider_ref": "smails#1"}, CONF)
            mailbox = provider.create_mailbox()
            message = provider.fetch_latest_message(mailbox)
        self.assertEqual(mailbox["token"], "mailbox-token")
        self.assertEqual(message["text_content"], "123456")
        self.assertEqual(session.calls[1]["headers"]["Authorization"], "Bearer mailbox-token")
        self.assertEqual(session.calls[2]["headers"]["Authorization"], "Bearer mailbox-token")

    def test_factory_rejects_unverified_provider_types(self):
        for name in ("mohmol", "mailboxtemp"):
            with self.assertRaisesRegex(RuntimeError, "不支持"):
                mail_provider._create_provider({"providers": [{"type": name, "enable": True}]})

    def test_agentmail_creates_inbox_and_reads_details_with_bearer_auth(self):
        session = FakeSession([
            {"inbox_id": "inbox-1", "email": "box@agentmail.test"},
            {"count": 2, "messages": [
                {"message_id": "wrong", "to": "other@agentmail.test", "subject": "wrong"},
            ], "next_page_token": "next-1"},
            {"messages": [{"message_id": "a1", "to": "box@agentmail.test", "subject": "Code", "timestamp": "2026-06-10T12:00:00Z"}]},
            {"message_id": "a1", "inbox_id": "inbox-1", "from": "sender@example.test", "to": "box@agentmail.test", "subject": "Code", "extracted_text": "Verification code: 123456", "extracted_html": "<b>123456</b>", "timestamp": "2026-06-10T12:00:00Z"},
        ])
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.AgentMailProvider({"provider_ref": "agentmail#1", "api_key": "agent-test-key", "domain": ["agentmail.test"]}, CONF)
            mailbox = provider.create_mailbox("box")
            message = provider.fetch_latest_message(mailbox)
            provider.close()
        self.assertEqual(mailbox["inbox_id"], "inbox-1")
        self.assertEqual(message["message_id"], "a1")
        self.assertEqual(message["text_content"], "Verification code: 123456")
        self.assertEqual(message["html_content"], "<b>123456</b>")
        self.assertEqual(session.calls[0]["url"], "https://api.agentmail.to/v0/inboxes")
        self.assertEqual(session.calls[0]["json"], {"username": "box", "domain": "agentmail.test"})
        self.assertEqual(session.calls[1]["headers"]["Authorization"], "Bearer agent-test-key")
        self.assertEqual(session.calls[2]["params"]["page_token"], "next-1")
        self.assertEqual(session.calls[3]["headers"]["Authorization"], "Bearer agent-test-key")
        self.assertEqual(session.calls[1]["url"], "https://api.agentmail.to/v0/inboxes/inbox-1/messages")
        self.assertEqual(session.calls[3]["url"], "https://api.agentmail.to/v0/inboxes/inbox-1/messages/a1")

    def test_agentmail_allows_server_generated_username_and_empty_inbox(self):
        session = FakeSession([
            {"inbox_id": "inbox-2", "email": "random@agentmail.to"},
            {"count": 0, "messages": []},
        ])
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.AgentMailProvider({"provider_ref": "agentmail#1", "api_key": "key"}, CONF)
            mailbox = provider.create_mailbox()
            message = provider.fetch_latest_message(mailbox)
        self.assertEqual(session.calls[0]["json"], {})
        self.assertIsNone(message)

    def test_agentmail_rejects_missing_key_and_malformed_responses(self):
        with mock.patch.object(mail_provider, "_create_session", return_value=FakeSession([])):
            with self.assertRaisesRegex(RuntimeError, "api_key"):
                mail_provider.AgentMailProvider({"provider_ref": "agentmail#1"}, CONF)

        create_session = FakeSession([{"email": "missing-id@agentmail.to"}])
        with mock.patch.object(mail_provider, "_create_session", return_value=create_session):
            provider = mail_provider.AgentMailProvider({"provider_ref": "agentmail#1", "api_key": "key"}, CONF)
            with self.assertRaisesRegex(RuntimeError, "inbox_id"):
                provider.create_mailbox()

        error_session = FakeSession([FakeResponse({"detail": "unauthorized"}, status_code=401)])
        with mock.patch.object(mail_provider, "_create_session", return_value=error_session):
            provider = mail_provider.AgentMailProvider({"provider_ref": "agentmail#1", "api_key": "key"}, CONF)
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                provider.create_mailbox()

    def test_base_wait_for_code_scans_recent_messages(self):
        provider = mail_provider.BaseMailProvider({"wait_timeout": 1, "wait_interval": 0.2})
        mailbox = {"address": "box@example.test"}
        provider.fetch_recent_messages = mock.Mock(return_value=[
            {"provider": "test", "mailbox": mailbox["address"], "message_id": "new", "subject": "Welcome", "text_content": "hello"},
            {"provider": "test", "mailbox": mailbox["address"], "message_id": "old", "subject": "Code", "text_content": "Verification code: 123456"},
        ])
        self.assertEqual(provider.wait_for_code(mailbox), "123456")
