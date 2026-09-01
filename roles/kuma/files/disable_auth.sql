INSERT INTO setting (key, value, type)
SELECT 'disableAuth', 'true', 'general'
WHERE EXISTS (SELECT 1 FROM user)
ON CONFLICT(key) DO UPDATE SET value = 'true', type = 'general'
WHERE setting.value != 'true';
SELECT changes();
