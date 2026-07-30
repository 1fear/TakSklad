-- Шаг 0 плана docs/kiz-lifecycle-integrity-architecture-plan-2026-07-30.md (§23).
-- Назначение: четыре read-only проверки, закрывающие пункты 4, 5, 6 и 7 шага 0.
--   A. Канонические определения и query pack инвариантов (§5, §15)
--   B. Distinct payment_type и расхождение двух классификаторов (6.15)
--   C. Доказательства записи вне service layer, writer registry (§14.1, §3)
--   D. Доля Transfer-заказов без разрешимого юридического получателя (7.4)
--
-- Все запросы только читают. Ни DDL, ни DML, ни временных таблиц, ни SET вне сессии.
-- Запускать так, чтобы read-only гарантировала сама сессия, а не дисциплина:
--
--   PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=120000' \
--     psql "$DSN" -v ON_ERROR_STOP=1 -P pager=off \
--     -f backend/sql/preflight_step0_kiz_lifecycle.sql
--
-- Полные коды маркировки сознательно не выбираются: вместо кода выводится
-- fingerprint left(md5(code),12) и длина. Это правило 7.1.10 плана.
--
-- Определения зеркалят код, а не придуманы заново:
--   последнее движение КИЗа  -> backend/app/kiz_movements_service.py:91,106
--                               backend/app/skladbot_daily_kiz.py:292
--     row_number() over (partition by kiz_id order by occurred_at desc, id desc) = 1
--   активные типы движения   -> backend/app/skladbot_daily_kiz.py:53
--     ('outbound','re_outbound')
--   освобождающие типы       -> backend/app/kiz_movements_service.py:16
--     ('return','undo','reset')
--   активный скан            -> backend/app/skladbot_daily_kiz.py:258-273
--     последнее движение кода активно И latest.scan_code_id = scan.id
--                              И latest.order_item_id = scan.order_item_id
--   текущий владелец         -> пара (order_id, order_item_id) активного движения
--   классификатор оплаты     -> backend/app/reports_service.py:550
--                               backend/app/skladbot_contracts.py:89,161
--   ключ клиента             -> backend/app/client_points_service.py:37,692
--     regexp_replace(lower(replace(x,'ё','е')), '[^0-9a-zа-я]+','','g')
--
-- Каждый запрос самодостаточен: prelude CTE повторяется, потому что psql выполняет
-- операторы независимо и CTE между ними не переносятся.


-- =====================================================================
-- A. ОПРЕДЕЛЕНИЯ И QUERY PACK ИНВАРИАНТОВ
-- =====================================================================

-- A0. Контрольные объёмы. Сверять с §5 документа: расхождение означает, что
-- baseline снят на других данных и остальные числа сравнивать нельзя.
SELECT
    (SELECT count(*) FROM orders)                          AS orders,
    (SELECT count(*) FROM order_items)                     AS order_items,
    (SELECT count(*) FROM scan_codes)                       AS scan_rows,
    (SELECT count(*) FROM kiz_codes)                        AS kiz_codes,
    (SELECT count(*) FROM kiz_movements)                    AS movements,
    (SELECT count(*) FROM audit_log)                        AS audit_rows,
    (SELECT count(*) FROM pending_events)                   AS pending_events,
    (SELECT count(*) FROM (
        SELECT DISTINCT ON (kiz_id) kiz_id, movement_type
        FROM kiz_movements
        ORDER BY kiz_id, occurred_at DESC, id DESC
     ) t WHERE movement_type IN ('outbound','re_outbound'))  AS kiz_active,
    now()                                                    AS snapshot_at;


-- A1. Инвариант: КИЗ с несколькими активными владельцами. Ожидается 0.
-- Определение активного владельца зеркалит ОБЕ ветки daily-гидрации
-- (backend/app/skladbot_daily_kiz.py:258-273), это принципиально:
--   ветка 1: у кода нет ни одного движения -> активны ВСЕ его строки скана,
--            именно здесь и достигается фатальный daily_kiz_duplicate_active_code;
--   ветка 2: последнее движение активно -> активна только строка, на которую
--            указывает latest.scan_code_id.
-- Запрос выводит обе метрики: сколько строк считаются активными и сколько строк
-- скана существует у кода вообще.
WITH latest AS (
    SELECT DISTINCT ON (m.kiz_id)
           m.kiz_id, m.movement_type, m.order_id, m.order_item_id, m.scan_code_id
    FROM kiz_movements m
    ORDER BY m.kiz_id, m.occurred_at DESC, m.id DESC
),
scan_owner AS (
    SELECT s.id AS scan_id, btrim(s.code) AS code, s.order_item_id, i.order_id
    FROM scan_codes s
    JOIN order_items i ON i.id = s.order_item_id
),
resolved AS (
    SELECT
        so.code,
        so.scan_id,
        so.order_id,
        l.kiz_id,
        l.movement_type,
        CASE
            WHEN l.kiz_id IS NULL THEN true
            WHEN l.movement_type IN ('outbound','re_outbound') AND l.scan_code_id = so.scan_id THEN true
            ELSE false
        END AS counts_as_active_owner
    FROM scan_owner so
    LEFT JOIN kiz_codes k ON k.code = so.code
    LEFT JOIN latest l ON l.kiz_id = k.id
)
SELECT
    left(md5(code), 12)                                            AS kiz_fp,
    length(code)                                                   AS code_length,
    count(*) FILTER (WHERE counts_as_active_owner)                 AS active_owner_rows,
    count(*)                                                       AS scan_rows_total,
    count(DISTINCT order_id) FILTER (WHERE counts_as_active_owner) AS distinct_active_orders,
    coalesce(max(movement_type), 'NO_MOVEMENT')                    AS latest_movement_type
FROM resolved
GROUP BY 1, 2
HAVING count(*) FILTER (WHERE counts_as_active_owner) > 1
ORDER BY 3 DESC, 1;


-- A2. Инвариант: активное последнее движение без строки активного скана.
-- Ожидается 0. Это тот отказ, который daily-гидрация поднимает как
-- daily_kiz_active_scan_missing: отчёт по такому коду построить нельзя.
WITH latest AS (
    SELECT DISTINCT ON (m.kiz_id)
           m.kiz_id, m.movement_type, m.order_id, m.order_item_id, m.scan_code_id, m.occurred_at
    FROM kiz_movements m
    ORDER BY m.kiz_id, m.occurred_at DESC, m.id DESC
)
SELECT
    left(md5(btrim(k.code)), 12) AS kiz_fp,
    length(btrim(k.code))        AS code_length,
    l.movement_type,
    l.occurred_at,
    (l.scan_code_id IS NULL)     AS movement_scan_ref_is_null,
    o.status                     AS order_status,
    o.payment_type
FROM latest l
JOIN kiz_codes k ON k.id = l.kiz_id
LEFT JOIN scan_codes s ON s.id = l.scan_code_id
LEFT JOIN order_items i ON i.id = l.order_item_id
LEFT JOIN orders o ON o.id = l.order_id
WHERE l.movement_type IN ('outbound','re_outbound')
  AND s.id IS NULL
ORDER BY l.occurred_at DESC;


-- A3. Инвариант: строка скана без активного последнего движения.
-- Это класс из 6.3 (81 строка на момент составления документа): строка существует
-- и попадает в отчёты по scan_codes, но КИЗ уже освобождён или отдан другому заказу.
WITH latest AS (
    SELECT DISTINCT ON (m.kiz_id)
           m.kiz_id, m.movement_type, m.order_item_id, m.scan_code_id
    FROM kiz_movements m
    ORDER BY m.kiz_id, m.occurred_at DESC, m.id DESC
),
scan_owner AS (
    SELECT s.id AS scan_id, btrim(s.code) AS code, s.order_item_id,
           i.order_id, o.status AS order_status, o.payment_type
    FROM scan_codes s
    JOIN order_items i ON i.id = s.order_item_id
    JOIN orders o ON o.id = i.order_id
)
SELECT
    CASE
        -- строка есть, ledger-записи нет вообще: daily считает такую строку активной,
        -- но происхождение строки не подтверждено движением, это отдельный класс
        WHEN l.kiz_id IS NULL THEN 'no_movement_at_all_daily_treats_active'
        WHEN l.movement_type IN ('return','undo','reset') THEN 'released_' || l.movement_type
        WHEN l.scan_code_id IS DISTINCT FROM so.scan_id THEN 'active_but_other_scan_row'
        ELSE 'other'
    END                                   AS reason,
    so.order_status,
    CASE
        WHEN lower(replace(btrim(coalesce(so.payment_type,'')), 'ё','е')) LIKE '%терминал%'
          OR lower(replace(btrim(coalesce(so.payment_type,'')), 'ё','е')) LIKE '%terminal%'
            THEN 'terminal'
        WHEN lower(replace(btrim(coalesce(so.payment_type,'')), 'ё','е')) LIKE '%перечис%'
          OR lower(replace(btrim(coalesce(so.payment_type,'')), 'ё','е')) LIKE '%безнал%'
          OR lower(replace(btrim(coalesce(so.payment_type,'')), 'ё','е')) LIKE '%transfer%'
            THEN 'transfer'
        ELSE 'unknown'
    END                                   AS payment_group,
    count(*)                              AS scan_rows
FROM scan_owner so
LEFT JOIN kiz_codes k ON k.code = so.code
LEFT JOIN latest l ON l.kiz_id = k.id
WHERE l.kiz_id IS NULL
   OR l.movement_type NOT IN ('outbound','re_outbound')
   OR l.scan_code_id IS DISTINCT FROM so.scan_id
GROUP BY 1, 2, 3
ORDER BY 4 DESC;


-- A4. Инвариант: заказ по Перечислению с освобождающим движением. Ожидается 0
-- после включения guard шага 3. Сейчас показывает фактический масштаб 6.1.
SELECT
    m.movement_type,
    o.status                                   AS order_status,
    count(*)                                   AS movements,
    count(DISTINCT o.id)                       AS orders,
    count(DISTINCT m.kiz_id)                   AS kiz_codes,
    min(m.occurred_at)                         AS first_seen,
    max(m.occurred_at)                         AS last_seen
FROM kiz_movements m
JOIN orders o ON o.id = m.order_id
WHERE m.movement_type IN ('return','undo','reset')
  AND (
        lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%перечис%'
     OR lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%безнал%'
     OR lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%transfer%'
      )
  AND lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) NOT LIKE '%терминал%'
  AND lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) NOT LIKE '%terminal%'
GROUP BY 1, 2
ORDER BY 3 DESC;


-- A5. Инвариант: закрытый заказ с маркируемой позицией без сканов. Ожидается 0.
-- Класс 6.6 и 6.9: позиция требует КИЗ, план не нулевой, строк скана нет.
SELECT
    o.status                                     AS order_status,
    coalesce((o.raw_payload->>'completed_without_kiz')::text, 'false') AS completed_without_kiz,
    count(*)                                     AS items,
    count(DISTINCT o.id)                         AS orders,
    sum(i.quantity_blocks)                       AS planned_blocks
FROM order_items i
JOIN orders o ON o.id = i.order_id
LEFT JOIN scan_codes s ON s.order_item_id = i.id
WHERE o.status IN ('completed','done','closed','returned','archived_no_kiz','cancelled')
  AND i.requires_kiz
  AND i.quantity_blocks > 0
  AND i.status <> 'removed_from_google_sheet'
  AND s.id IS NULL
GROUP BY 1, 2
ORDER BY 3 DESC;


-- A6. Инвариант 6.13: движения с обнулёнными ссылками. Ожидается 0.
-- ON DELETE SET NULL уже сработал там, где значение null у активного движения.
SELECT
    movement_type,
    count(*)                                                   AS movements,
    count(*) FILTER (WHERE order_id IS NULL)                   AS order_ref_null,
    count(*) FILTER (WHERE order_item_id IS NULL)              AS item_ref_null,
    count(*) FILTER (WHERE scan_code_id IS NULL)               AS scan_ref_null
FROM kiz_movements
GROUP BY 1
ORDER BY 2 DESC;


-- A7. Инвариант 6.14: совпадение occurred_at внутри одного КИЗа. Ожидается 0.
-- При совпадении порядок определяется случайным UUID, то есть семантическая
-- вершина выбирается произвольно.
SELECT
    left(md5(btrim(k.code)), 12) AS kiz_fp,
    m.occurred_at,
    count(*)                     AS movements_at_same_instant,
    array_agg(m.movement_type ORDER BY m.id) AS movement_types
FROM kiz_movements m
JOIN kiz_codes k ON k.id = m.kiz_id
GROUP BY 1, 2
HAVING count(*) > 1
ORDER BY 3 DESC, 2 DESC;


-- A8. Инвариант 6.19: расхождение денормализованного счётчика и пересчёта по сканам.
-- Ожидается 0. block_quantity берётся из raw_payload скана, как это делает
-- backend/app/scan_quantities.py:91, с падением на 1 при отсутствии значения.
WITH recount AS (
    SELECT
        s.order_item_id,
        sum(GREATEST(1, coalesce(NULLIF(s.raw_payload->>'block_quantity','')::int, 1))) AS recounted_blocks,
        count(*)                                                                        AS scan_rows
    FROM scan_codes s
    GROUP BY s.order_item_id
)
SELECT
    o.status                                        AS order_status,
    count(*)                                        AS items,
    count(DISTINCT o.id)                            AS orders,
    sum(abs(i.scanned_blocks - coalesce(r.recounted_blocks, 0))) AS total_drift
FROM order_items i
JOIN orders o ON o.id = i.order_id
LEFT JOIN recount r ON r.order_item_id = i.id
WHERE i.scanned_blocks <> coalesce(r.recounted_blocks, 0)
GROUP BY 1
ORDER BY 2 DESC;


-- A9. Инвариант 6.8: подозрительный canonical-формат кода. Ожидается 0.
-- Эвристики, а не нормализатор: короткие коды и признак склеенных кодов, когда
-- префикс товарной группы встречается в строке больше одного раза.
SELECT
    CASE
        WHEN length(btrim(code)) < 18 THEN 'too_short'
        WHEN (length(btrim(code)) - length(replace(btrim(code), '0104', ''))) / 4 > 1 THEN 'compound_suspect'
        ELSE 'other'
    END                            AS anomaly,
    left(md5(btrim(code)), 12)     AS kiz_fp,
    length(btrim(code))            AS code_length,
    (SELECT count(*) FROM scan_codes s WHERE btrim(s.code) = btrim(kc.code)) AS scan_rows
FROM kiz_codes kc
WHERE length(btrim(code)) < 18
   OR (length(btrim(code)) - length(replace(btrim(code), '0104', ''))) / 4 > 1
ORDER BY 1, 3;


-- =====================================================================
-- B. PAYMENT_TYPE: DISTINCT И РАСХОЖДЕНИЕ КЛАССИФИКАТОРОВ (6.15)
-- =====================================================================

-- B1. Все distinct-значения с обеими классификациями.
-- reports_service.payment_group принимает латиницу, skladbot_contracts
-- normalize_payment_type её не принимает; порядок проверок сохранён: терминал первым.
WITH normalized AS (
    SELECT
        o.payment_type,
        o.status,
        lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) AS p_reports,
        btrim(regexp_replace(
            regexp_replace(lower(replace(coalesce(o.payment_type,''), 'ё','е')), '[^0-9a-zа-я]+', ' ', 'g'),
            '\s+', ' ', 'g')) AS p_skladbot
    FROM orders o
),
classified AS (
    SELECT
        payment_type,
        status,
        CASE
            WHEN p_reports LIKE '%терминал%' OR p_reports LIKE '%terminal%' THEN 'terminal'
            WHEN p_reports LIKE '%перечис%' OR p_reports LIKE '%безнал%' OR p_reports LIKE '%transfer%' THEN 'transfer'
            ELSE 'unknown'
        END AS group_reports,
        CASE
            WHEN p_skladbot LIKE '%терминал%' THEN 'terminal'
            WHEN p_skladbot LIKE '%перечис%' OR p_skladbot LIKE '%безнал%' THEN 'transfer'
            ELSE 'unknown'
        END AS group_skladbot
    FROM normalized
)
SELECT
    payment_type,
    group_reports,
    group_skladbot,
    (group_reports <> group_skladbot)                          AS classifiers_disagree,
    count(*)                                                    AS orders,
    count(*) FILTER (WHERE status NOT IN
        ('completed','done','closed','returned','archived_no_kiz','cancelled'))  AS active_orders
FROM classified
GROUP BY 1, 2, 3, 4
ORDER BY 4 DESC, 5 DESC;


-- B2. Сводка для gate шага 1: доля unknown и доля расхождений.
-- Gate: unknown_active = 0 либо каждое значение разобрано вручную.
WITH classified AS (
    SELECT
        o.id,
        o.status,
        CASE
            WHEN lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%терминал%'
              OR lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%terminal%' THEN 'terminal'
            WHEN lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%перечис%'
              OR lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%безнал%'
              OR lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%transfer%' THEN 'transfer'
            ELSE 'unknown'
        END AS group_reports,
        CASE
            WHEN btrim(regexp_replace(regexp_replace(lower(replace(coalesce(o.payment_type,''), 'ё','е')),
                 '[^0-9a-zа-я]+',' ','g'), '\s+',' ','g')) LIKE '%терминал%' THEN 'terminal'
            WHEN btrim(regexp_replace(regexp_replace(lower(replace(coalesce(o.payment_type,''), 'ё','е')),
                 '[^0-9a-zа-я]+',' ','g'), '\s+',' ','g')) LIKE '%перечис%'
              OR btrim(regexp_replace(regexp_replace(lower(replace(coalesce(o.payment_type,''), 'ё','е')),
                 '[^0-9a-zа-я]+',' ','g'), '\s+',' ','g')) LIKE '%безнал%' THEN 'transfer'
            ELSE 'unknown'
        END AS group_skladbot
    FROM orders o
)
SELECT
    count(*)                                                            AS orders_total,
    count(*) FILTER (WHERE group_reports = 'unknown')                   AS unknown_total,
    count(*) FILTER (WHERE group_reports = 'unknown'
        AND status NOT IN ('completed','done','closed','returned','archived_no_kiz','cancelled'))
                                                                        AS unknown_active,
    count(*) FILTER (WHERE group_reports <> group_skladbot)             AS disagreements,
    count(DISTINCT CASE WHEN group_reports <> group_skladbot THEN id END) AS disagreement_orders
FROM classified;


-- =====================================================================
-- C. ЗАПИСЬ ВНЕ SERVICE LAYER, WRITER REGISTRY (§3, §14.1)
-- =====================================================================

-- C1. Строки скана без audit-записи создания. create_scan всегда пишет
-- scan_code_created (backend/app/orders_service.py:337), repair-скрипты пишут
-- собственные action. Строка без любой из этих записей означает запись вне API.
SELECT
    coalesce(a.action, 'NO_AUDIT_RECORD') AS creating_action,
    count(*)                              AS scan_rows,
    min(s.scanned_at)                     AS first_scan,
    max(s.scanned_at)                     AS last_scan
FROM scan_codes s
LEFT JOIN audit_log a
       ON a.entity_type = 'scan_code'
      AND a.entity_id = s.id::text
      AND a.action IN ('scan_code_created', 'google_cutover_active_kiz_scan_repaired')
GROUP BY 1
ORDER BY 2 DESC;


-- C2. Движения по источнику, актору и наличию служебных полей payload.
-- create_scan всегда пишет scan_source в raw_payload движения
-- (backend/app/orders_service.py:306). Активное движение без этого ключа
-- сделано другим writer-ом.
SELECT
    movement_type,
    source,
    coalesce(actor, 'NULL')                                       AS actor,
    (raw_payload ? 'scan_source')                                 AS has_scan_source,
    (raw_payload ? 'reason')                                      AS has_repair_reason,
    count(*)                                                      AS movements,
    min(occurred_at)                                              AS first_seen,
    max(occurred_at)                                              AS last_seen
FROM kiz_movements
GROUP BY 1, 2, 3, 4, 5
ORDER BY 6 DESC;


-- C3. Смена статуса заказа без audit-записи. Проверка гипотезы 2 из §3:
-- заказ сейчас активен, но последняя известная admin-операция закрывала его,
-- и записи о повторном открытии нет.
WITH last_action AS (
    SELECT DISTINCT ON (a.entity_id)
           a.entity_id, a.action, a.created_at
    FROM audit_log a
    WHERE a.entity_type = 'order'
      AND a.action IN ('order_completed','order_completed_without_kiz','order_returned',
                       'order_cancelled','order_archived_without_kiz',
                       'order_reset_for_rescan','order_restored')
    ORDER BY a.entity_id, a.created_at DESC
)
SELECT
    la.action                       AS last_audited_action,
    o.status                        AS current_status,
    count(*)                        AS orders,
    max(la.created_at)              AS last_action_at
FROM orders o
JOIN last_action la ON la.entity_id = o.id::text
WHERE (la.action IN ('order_completed','order_completed_without_kiz')
       AND o.status NOT IN ('completed','done','closed','returned'))
   OR (la.action = 'order_returned' AND o.status <> 'returned')
   OR (la.action = 'order_cancelled' AND o.status <> 'cancelled')
   OR (la.action = 'order_archived_without_kiz' AND o.status <> 'archived_no_kiz')
GROUP BY 1, 2
ORDER BY 3 DESC;


-- C4. Кто физически может писать в ledger. Нужно для решения «часть DB-рубежей
-- в первый change set»: если runtime-роль имеет UPDATE/DELETE, guard обходится.
SELECT
    current_user                                                AS connected_as,
    session_user                                                AS session_role,
    has_table_privilege(current_user, 'kiz_movements', 'UPDATE') AS can_update_movements,
    has_table_privilege(current_user, 'kiz_movements', 'DELETE') AS can_delete_movements,
    has_table_privilege(current_user, 'scan_codes', 'DELETE')    AS can_delete_scans,
    has_table_privilege(current_user, 'orders', 'DELETE')        AS can_delete_orders;

-- C5. Все грантополучатели на таблицах жизненного цикла КИЗа.
SELECT
    g.table_name,
    g.grantee,
    string_agg(DISTINCT g.privilege_type, ',' ORDER BY g.privilege_type) AS privileges
FROM information_schema.role_table_grants g
WHERE g.table_name IN ('kiz_movements','kiz_codes','scan_codes','order_items','orders','audit_log')
GROUP BY 1, 2
ORDER BY 1, 2;

-- C6. Роли, которыми можно войти в кластер. Пароли и секреты не выбираются.
SELECT
    rolname,
    rolsuper,
    rolcanlogin,
    rolbypassrls
FROM pg_roles
WHERE rolcanlogin
ORDER BY rolsuper DESC, rolname;


-- =====================================================================
-- D. TRANSFER-ЗАКАЗЫ БЕЗ РАЗРЕШИМОГО ЮРИДИЧЕСКОГО ПОЛУЧАТЕЛЯ (7.4)
-- =====================================================================

-- D1. Сводка: сколько Transfer-заказов имеет разрешимый стабильный ключ клиента.
-- Ключ считается ровно так, как это делает приложение при сопоставлении заказа
-- с точкой клиента (backend/app/client_points_service.py:37).
-- Это проверка допущения «правило 7.4.2 остановит доставку»: смотреть на
-- transfer_without_key_share.
WITH transfer_orders AS (
    SELECT
        o.id,
        o.status,
        o.client,
        regexp_replace(lower(replace(coalesce(o.client,''), 'ё','е')), '[^0-9a-zа-я]+', '', 'g') AS client_key,
        btrim(coalesce(o.raw_payload->>'skladbot_request_id','')) AS request_id,
        btrim(coalesce(o.raw_payload->>'skladbot_request_number','')) AS request_number
    FROM orders o
    WHERE (lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%перечис%'
        OR lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%безнал%'
        OR lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%transfer%')
      AND lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) NOT LIKE '%терминал%'
      AND lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) NOT LIKE '%terminal%'
)
SELECT
    count(*)                                                              AS transfer_orders,
    count(*) FILTER (WHERE client_key = '')                               AS empty_client,
    count(*) FILTER (WHERE NOT EXISTS (
        SELECT 1 FROM client_points cp WHERE cp.normalized_client = t.client_key))
                                                                          AS without_client_point,
    round(100.0 * count(*) FILTER (WHERE NOT EXISTS (
        SELECT 1 FROM client_points cp WHERE cp.normalized_client = t.client_key))
        / NULLIF(count(*), 0), 1)                                         AS transfer_without_key_share,
    count(*) FILTER (WHERE request_id = '' OR request_number = '')         AS without_full_skladbot_pair,
    count(DISTINCT client_key)                                            AS distinct_client_keys
FROM transfer_orders t;


-- D2. Разрез по статусу заказа: важно, сколько незакрытых Transfer-заказов
-- попадёт под seal сразу после включения ступени B из 7.4.
WITH transfer_orders AS (
    SELECT
        o.id,
        o.status,
        regexp_replace(lower(replace(coalesce(o.client,''), 'ё','е')), '[^0-9a-zа-я]+', '', 'g') AS client_key
    FROM orders o
    WHERE (lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%перечис%'
        OR lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%безнал%'
        OR lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) LIKE '%transfer%')
      AND lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) NOT LIKE '%терминал%'
      AND lower(replace(btrim(coalesce(o.payment_type,'')), 'ё','е')) NOT LIKE '%terminal%'
)
SELECT
    t.status,
    count(*)                                                     AS orders,
    count(*) FILTER (WHERE EXISTS (
        SELECT 1 FROM client_points cp WHERE cp.normalized_client = t.client_key)) AS with_client_point,
    count(*) FILTER (WHERE NOT EXISTS (
        SELECT 1 FROM client_points cp WHERE cp.normalized_client = t.client_key)) AS without_client_point
FROM transfer_orders t
GROUP BY 1
ORDER BY 2 DESC;


-- D3. Один клиент, записанный несколькими вариантами. Класс 6.7: варианты
-- написания создают разные строки, а стабильного идентификатора нет.
-- Полные наименования выводятся: это внутренняя диагностика, не client-facing вывод.
SELECT
    regexp_replace(lower(replace(coalesce(client,''), 'ё','е')), '[^0-9a-zа-я]+', '', 'g') AS client_key,
    count(DISTINCT client)                       AS spelling_variants,
    count(*)                                     AS orders,
    array_agg(DISTINCT client ORDER BY client)   AS variants
FROM orders
WHERE coalesce(btrim(client), '') <> ''
GROUP BY 1
HAVING count(DISTINCT client) > 1
ORDER BY 2 DESC, 3 DESC;


-- D4. Разные ключи клиента, различающиеся только латинскими гомоглифами.
-- Нормализатор приложения оставляет латиницу как есть, поэтому «Ромашка» с
-- латинской «a» даёт другой ключ и молча не находит точку клиента. Для legal
-- entity resolution это источник ложных «неразрешимых» получателей, для
-- alias-политики 13.2 это запрещённый к автослиянию случай, который всё равно
-- нужно видеть глазами.
WITH keys AS (
    SELECT
        regexp_replace(lower(replace(coalesce(client,''), 'ё','е')), '[^0-9a-zа-я]+', '', 'g') AS client_key,
        client,
        count(*) AS orders
    FROM orders
    WHERE coalesce(btrim(client), '') <> ''
    GROUP BY 1, 2
),
folded AS (
    SELECT
        translate(client_key, 'aeopcyxbkmhtin', 'аеорсухвкмнтiн') AS folded_key,
        client_key,
        client,
        orders
    FROM keys
)
SELECT
    folded_key,
    count(DISTINCT client_key)                        AS distinct_keys,
    sum(orders)                                       AS orders,
    array_agg(DISTINCT client_key ORDER BY client_key) AS keys,
    array_agg(DISTINCT client ORDER BY client)         AS variants
FROM folded
GROUP BY 1
HAVING count(DISTINCT client_key) > 1
ORDER BY 2 DESC, 3 DESC;
