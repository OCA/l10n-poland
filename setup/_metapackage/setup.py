import setuptools

with open('VERSION.txt', 'r') as f:
    version = f.read().strip()

setuptools.setup(
    name="odoo14-addons-oca-l10n-poland",
    description="Meta package for oca-l10n-poland Odoo addons",
    version=version,
    install_requires=[
        'odoo14-addon-currency_rate_update_nbp',
    ],
    classifiers=[
        'Programming Language :: Python',
        'Framework :: Odoo',
        'Framework :: Odoo :: 14.0',
    ]
)
