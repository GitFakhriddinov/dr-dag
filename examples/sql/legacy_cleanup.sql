-- Old cleanup step, kept as a bad example for `dagdoctor review`.
DELETE FROM staging.partner_orders;

SELECT * FROM staging.partner_orders_backup;
