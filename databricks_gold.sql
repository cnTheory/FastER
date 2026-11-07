-- Gold table for FastER: risk-adjusted outcome index by hospital x condition group.
-- Index = observed deaths / expected deaths, where expected uses the STATE mortality rate for the
-- same condition AND APR severity code. 1.00 = state average, <1 better, >1 worse.
-- VERIFY the apr_mdc_description values in your data before trusting the CASE mapping.
CREATE OR REPLACE TABLE faster.gold.outcomes AS
WITH base AS (
  SELECT facility_id,
         CASE
           WHEN apr_mdc_description LIKE '%Circulatory%'  THEN 'cardiac'
           WHEN apr_mdc_description LIKE '%Nervous%'      THEN 'neuro'
           WHEN apr_mdc_description LIKE '%Respiratory%'  THEN 'respiratory'
           WHEN apr_mdc_description LIKE '%Infectious%'   THEN 'infection'
           WHEN apr_mdc_description LIKE '%Digestive%'    THEN 'abdominal'
           WHEN apr_mdc_description LIKE '%Injur%' OR apr_mdc_description LIKE '%Trauma%'
             OR apr_mdc_description LIKE '%Burn%' OR apr_mdc_description LIKE '%Musculoskeletal%' THEN 'trauma'
         END AS cond,
         apr_severity_of_illness_code AS sev,
         CASE WHEN patient_disposition = 'Expired' THEN 1 ELSE 0 END AS died
  FROM faster.silver.sparcs_ip_2024            -- emergency admissions, facility_id not null
),
state_rate AS (
  SELECT cond, sev, AVG(died) AS p FROM base WHERE cond IS NOT NULL GROUP BY cond, sev
),
hosp AS (
  SELECT b.facility_id, b.cond, COUNT(*) AS n, SUM(b.died) AS observed, SUM(s.p) AS expected
  FROM base b JOIN state_rate s ON b.cond = s.cond AND b.sev = s.sev
  WHERE b.cond IS NOT NULL
  GROUP BY b.facility_id, b.cond
)
SELECT facility_id, cond, n,
       CASE WHEN n < 30 THEN NULL                       -- suppress small cells
            ELSE (observed + 2) / (expected + 2) END AS outcome_index   -- light shrinkage toward 1.0
FROM hosp;
