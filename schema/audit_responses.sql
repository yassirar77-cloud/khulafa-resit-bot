create table public.audit_responses (
  id bigint generated always as identity primary key,
  receipt_id bigint references receipts(id) on delete cascade,
  chat_id bigint,
  question_type text,
  question_text text,
  question_message_id bigint,
  manager_reply text,
  asked_at timestamptz default now(),
  replied_at timestamptz
);

create index if not exists audit_responses_chat_msg_idx
  on public.audit_responses (chat_id, question_message_id);
