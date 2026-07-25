import pandas as pd

transactions = pd.read_csv("HI-Small_Trans.csv")
accounts = pd.read_csv("HI-Small_accounts.csv")


print(transactions.head())

print(transactions.columns)

print(transactions.info())

print(transactions.describe())

print(accounts.head())

print(accounts.columns)
