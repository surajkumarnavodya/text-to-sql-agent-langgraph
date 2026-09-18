# Sales Data Guide

> **Disclosure**: this document describes this project's own
> "adventureworks" sample database (an AdventureWorksDW-shaped schema),
> verified against it via live schema introspection when this file was
> written. It is a demonstration/placeholder document for this project's
> vector-retrieval feature, not real production data-governance
> documentation -- see `docs/vector-retrieval-design.md`.

## Fact tables and their grain

This database has two separate sales fact tables, and questions about
"sales" should be routed to the one that actually matches the question:

- **FactInternetSales** -- one row per order line placed by an individual
  end customer (`DimCustomer`) through the online/direct channel. Grain:
  `SalesOrderNumber` + `SalesOrderLineNumber`.
- **FactResellerSales** -- one row per order line placed by a reseller
  business (`DimReseller`), via a sales employee (`DimEmployee`). Same
  grain shape, different dimensions.

Never `UNION` these two fact tables without being asked explicitly to
combine both channels -- they have different dimension keys (a reseller
order has no `CustomerKey`; an internet order has no `ResellerKey`/
`EmployeeKey`), so a careless union produces a result with a mix of NULLs
that misrepresents the data.

Two smaller, more specialized fact tables exist alongside the two above:

- **FactSalesQuota** -- one row per employee per calendar quarter, holding
  their assigned sales quota. Compare against *aggregated* actual sales for
  the same employee/quarter, never against a full year or a different
  grain.
- **FactCurrencyRate** -- daily currency conversion rates. A newer table,
  **NewFactCurrencyRate**, also exists with a similar shape but a different
  column layout (`CurrencyID`, `CurrencyDate` instead of `CurrencyKey`,
  `Date`) -- prefer `FactCurrencyRate` unless a question specifically asks
  about the newer feed, since `FactCurrencyRate` is what every other
  currency-aware fact table's `CurrencyKey` actually joins against.

## The product hierarchy

Products are organized in three levels: `DimProductCategory` (e.g.
"Bikes") -> `DimProductSubcategory` (e.g. "Road Bikes") -> `DimProduct`
(e.g. a specific bike model/color/size). There is **no direct foreign key**
from `DimProduct` to `DimProductCategory` -- every query that needs the
category must join through `DimProductSubcategory` as an intermediate
table.

## Date handling

Every fact table's date columns (`OrderDateKey`, `DueDateKey`,
`ShipDateKey`) are integer surrogate keys into `DimDate`, not native date
columns -- always join to `DimDate` to filter or group by calendar year,
quarter, month, or day name, rather than trying to parse the key integer
directly (its format, `YYYYMMDD` as an integer, is an implementation
detail that should not be relied upon in a `WHERE` clause).

`DimDate` carries both **calendar** (`CalendarYear`, `CalendarQuarter`,
`CalendarSemester`) and **fiscal** (`FiscalYear`, `FiscalQuarter`,
`FiscalSemester`) period columns. Unless a question explicitly says
"fiscal year" or "fiscal quarter," use the calendar columns.

## Tables to ignore for business questions

A few tables in this database exist for administrative/tooling purposes
and are never a good answer to a business question, even if their name or
columns happen to lexically match part of a question:

- `sysdiagrams` -- SQL Server's own database-diagram metadata table, not
  business data.
- `DatabaseLog` -- an audit trail of schema changes (DDL events), not a
  sales or customer activity log despite having `DatabaseUser` and `Event`
  columns that might superficially look relevant to an "activity" question.
- `AdventureWorksDWBuildVersion` -- a single-row table recording which
  schema-build version is installed, purely a deployment artifact.

## Geography and territory

`DimGeography` holds address-level location detail (city, state/province,
country, postal code) and is linked to both `DimCustomer` and `DimReseller`
via `GeographyKey`. `DimSalesTerritory` is a separate, coarser sales-region
grouping (e.g. "Northwest," "Canada," "Europe") used for reporting sales by
territory -- the two are related (`DimGeography.SalesTerritoryKey` points
at `DimSalesTerritory`) but answer different questions: "where is this
customer located" (`DimGeography`) versus "which sales region is this order
attributed to" (`DimSalesTerritory`, referenced directly by the fact
tables' own `SalesTerritoryKey`).
