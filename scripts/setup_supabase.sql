-- รันคำสั่งนี้ใน Supabase -> SQL Editor
-- โครงสร้างแยกแถวตาม Discord user_id (ฝั่งบอทใช้ SUPABASE_KEY แบบ Secret / service_role ซึ่งข้าม RLS อัตโนมัติ)

CREATE TABLE IF NOT EXISTS public.user_profiles (
    user_id TEXT PRIMARY KEY,
    watchlist JSONB NOT NULL DEFAULT '[]'::jsonb,
    portfolio JSONB NOT NULL DEFAULT '{}'::jsonb,
    alerts JSONB NOT NULL DEFAULT '[]'::jsonb,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 1. ตรวจสอบตารางใน schema public ที่ยังไม่เปิด RLS
SELECT tablename
FROM pg_tables
WHERE schemaname = 'public' AND rowsecurity = false;

-- 2. เปิด Row Level Security ให้ครบทุกตารางใน schema public
ALTER TABLE public.user_profiles ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
    tbl RECORD;
BEGIN
    FOR tbl IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public' AND rowsecurity = false
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', tbl.tablename);
    END LOOP;
END
$$;

-- 3. ลบ Policy เก่าที่อาจเปิดกว้างเกินไป แล้วสร้าง Policy ที่รัดกุม (SELECT, INSERT, UPDATE, DELETE)
--    อนุญาตเฉพาะผู้ใช้ที่ล็อกอิน (authenticated) และเป็นเจ้าของแถวเท่านั้น (ห้าม anon เข้าถึงทุกกรณี)
DROP POLICY IF EXISTS "Allow bot full access" ON public.user_profiles;
DROP POLICY IF EXISTS "Users read own profile" ON public.user_profiles;
DROP POLICY IF EXISTS "Users insert own profile" ON public.user_profiles;
DROP POLICY IF EXISTS "Users update own profile" ON public.user_profiles;
DROP POLICY IF EXISTS "Users delete own profile" ON public.user_profiles;

CREATE POLICY "Users read own profile" ON public.user_profiles
    FOR SELECT
    TO authenticated
    USING ((SELECT auth.uid())::text = user_id);

CREATE POLICY "Users insert own profile" ON public.user_profiles
    FOR INSERT
    TO authenticated
    WITH CHECK ((SELECT auth.uid())::text = user_id);

CREATE POLICY "Users update own profile" ON public.user_profiles
    FOR UPDATE
    TO authenticated
    USING ((SELECT auth.uid())::text = user_id)
    WITH CHECK ((SELECT auth.uid())::text = user_id);

CREATE POLICY "Users delete own profile" ON public.user_profiles
    FOR DELETE
    TO authenticated
    USING ((SELECT auth.uid())::text = user_id);

-- 4. ตรวจซ้ำหลังรัน: ต้องไม่มีแถวคืนกลับมา (rowsecurity = false = 0 ตาราง)
SELECT tablename
FROM pg_tables
WHERE schemaname = 'public' AND rowsecurity = false;
