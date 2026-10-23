-- รันคำสั่งนี้ใน Supabase -> SQL Editor เพียงครั้งเดียว
-- โครงสร้างแยกแถวตาม Discord user_id เพื่อความยืดหยุ่นสูงสุด รองรับการต่อยอดทำ Dashboard หรือตั้งค่ารายคนในอนาคต

CREATE TABLE IF NOT EXISTS public.user_profiles (
    user_id TEXT PRIMARY KEY,
    watchlist JSONB NOT NULL DEFAULT '[]'::jsonb,
    portfolio JSONB NOT NULL DEFAULT '{}'::jsonb,
    alerts JSONB NOT NULL DEFAULT '[]'::jsonb,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- เปิด Row Level Security (แนะนำให้ใช้ service_role key ใน SUPABASE_KEY ของบอท หรือเปิด Policy ด้านล่างหากใช้ anon key)
ALTER TABLE public.user_profiles ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = 'user_profiles' AND policyname = 'Allow bot full access'
    ) THEN
        CREATE POLICY "Allow bot full access" ON public.user_profiles
            FOR ALL
            USING (true)
            WITH CHECK (true);
    END IF;
END
$$;
