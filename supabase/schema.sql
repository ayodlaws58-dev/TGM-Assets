create table if not exists public.assets (
    asset_id text primary key,
    name text not null default '',
    category text not null default '',
    location text not null default '',
    last_service text not null default '',
    interval_days text not null default '',
    notes text not null default ''
);

create table if not exists public.work_orders (
    wo_id text primary key,
    asset_id text not null default '',
    title text not null default '',
    description text not null default '',
    priority text not null default '',
    status text not null default '',
    assigned_to text not null default '',
    created text not null default '',
    due text not null default '',
    completed text not null default '',
    requested_by text not null default '',
    cost text not null default '',
    payment_status text not null default '',
    paid_on text not null default '',
    approval_status text not null default '',
    approved_by text not null default '',
    approved_on text not null default '',
    decision_note text not null default ''
);

create table if not exists public.procurement (
    req_id text primary key,
    item text not null default '',
    quantity text not null default '',
    unit_cost text not null default '',
    asset_id text not null default '',
    wo_id text not null default '',
    reason text not null default '',
    requested_by text not null default '',
    created text not null default '',
    status text not null default '',
    decided_by text not null default '',
    decided_on text not null default '',
    decision_note text not null default ''
);

create table if not exists public.user_profiles (
    username text primary key,
    auth_uid uuid not null unique references auth.users(id) on delete cascade,
    full_name text not null default '',
    role text not null,
    email text not null unique,
    reset_salt text not null default '',
    reset_hash text not null default '',
    reset_expires text not null default '',
    reset_attempts text not null default ''
);

create table if not exists public.notifications (
    notif_id text primary key,
    username text not null default '',
    kind text not null default '',
    title text not null default '',
    message text not null default '',
    created text not null default '',
    read text not null default '',
    key text not null default ''
);

alter table public.assets enable row level security;
alter table public.work_orders enable row level security;
alter table public.procurement enable row level security;
alter table public.user_profiles enable row level security;
alter table public.notifications enable row level security;
