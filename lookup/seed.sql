-- Placeholder GL mapping. Replace account/class IDs with TEP's NetSuite internal IDs.
INSERT OR IGNORE INTO gl_map VALUES
 ('School supplies',       '1300', 'GIK Inventory', '4300', 'In-Kind Contributions - Supplies',   '101', 'Program: Resource Center'),
 ('Books & media',         '1300', 'GIK Inventory', '4310', 'In-Kind Contributions - Books',      '101', 'Program: Resource Center'),
 ('Technology',            '1300', 'GIK Inventory', '4320', 'In-Kind Contributions - Technology', '101', 'Program: Resource Center'),
 ('Furniture & equipment', '1500', 'Fixed Assets',  '4330', 'In-Kind Contributions - Equipment',  '200', 'Admin'),
 ('Apparel & hygiene',     '1300', 'GIK Inventory', '4340', 'In-Kind Contributions - Apparel',    '101', 'Program: Resource Center'),
 ('Household & lighting',  '1300', 'GIK Inventory', '4350', 'In-Kind Contributions - Household',  '101', 'Program: Resource Center'),
 ('Health & personal care','1300', 'GIK Inventory', '4360', 'In-Kind Contributions - Health',     '101', 'Program: Resource Center'),
 ('Food & beverage',       '1300', 'GIK Inventory', '4370', 'In-Kind Contributions - Food',       '101', 'Program: Resource Center'),
 ('Uncategorized',         '1300', 'GIK Inventory', '4390', 'In-Kind Contributions - Other',      '101', 'Program: Resource Center');

INSERT OR IGNORE INTO donors (name, type, ns_customer_id, contact) VALUES
 ('Sample Office Supply Co.', 'corporate', 'CUST-1001', 'donations@example.com'),
 ('Sample Freight Partners',  'freight-in-kind', 'CUST-1002', NULL);
