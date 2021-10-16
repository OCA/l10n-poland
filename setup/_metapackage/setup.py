import setuptools

with open('VERSION.txt', 'r') as f:
    version = f.read().strip()

setuptools.setup(
    name="odoo11-addons-oca-l10n-poland",
    description="Meta package for oca-l10n-poland Odoo addons",
    version=version,
    install_requires=[
        'odoo11-addon-account_bank_statement_import_mt940_pl_raiffeisen',
    ],
    classifiers=[
        'Programming Language :: Python',
        'Framework :: Odoo',
        'Framework :: Odoo :: 11.0',
    ]
)
