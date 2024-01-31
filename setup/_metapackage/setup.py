import setuptools

with open('VERSION.txt', 'r') as f:
    version = f.read().strip()

setuptools.setup(
    name="odoo-addons-oca-l10n-poland",
    description="Meta package for oca-l10n-poland Odoo addons",
    version=version,
    install_requires=[
        'odoo-addon-currency_rate_update_nbp>=16.0dev,<16.1dev',
    ],
    classifiers=[
        'Programming Language :: Python',
        'Framework :: Odoo',
        'Framework :: Odoo :: 16.0',
    ]
)
