{# Use custom schema names verbatim (raw, analytics) instead of dbt's default
   "<target_schema>_<custom>" so seeds land in the same `raw` schema as every
   other loader. #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}{{ target.schema }}{%- else -%}{{ custom_schema_name | trim }}{%- endif -%}
{%- endmacro %}
