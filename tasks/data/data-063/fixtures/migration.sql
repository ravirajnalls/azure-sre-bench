-- Migration V2026.10.26.01 — orders schema cleanup (change request CHG-2214)
-- Scheduled: 2026-10-26 (Monday) 02:15 UTC
-- Author: data-platform team
-- Target: sqldb-orders-prod (server: sql-shop-prod)

BEGIN TRANSACTION;

-- 1. Drop the old reporting indexes per CHG-2214 appendix B
DROP INDEX IF EXISTS IX_Orders_CustomerId ON dbo.Orders;
DROP INDEX IF EXISTS IX_Orders_ReportingTags ON dbo.Orders;

-- 2. Archive the legacy ReportingTags column (no callers since 2024)
ALTER TABLE dbo.Orders DROP COLUMN ReportingTags;

-- 3. New covering index for the invoicing service (see CHG-2214 appendix C)
CREATE NONCLUSTERED INDEX IX_Orders_InvoiceStatus
    ON dbo.Orders (InvoiceStatus, CreatedAtUtc)
    INCLUDE (TotalAmount);

COMMIT;

-- Notes:
--   Index list was generated from the 2023 reporting-workload survey.
--   Row counts: dbo.Orders ~ 1.2B rows, live table size 840 GB.
