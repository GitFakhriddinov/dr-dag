-- Daily revenue per store, rebuilt one day at a time.
INSERT INTO mart.shop_daily_sales
SELECT
    toDate(created_at) AS day,
    store_id,
    count()            AS orders,
    sum(amont)         AS revenue
FROM raw.orders
WHERE toDate(created_at) = {ds:Date}
  AND status = 'paid'
GROUP BY day, store_id
