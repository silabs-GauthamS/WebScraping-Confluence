import win32com.client

outlook = win32com.client.Dispatch("Outlook.Application")
mail = outlook.CreateItem(0)

mail.To = "VenkataRamanaKumar.Rajanala@silabs.com; Gautham.Sharma@silabs.com"
mail.Subject = "Weekly Test Report"
mail.HTMLBody = """
<h2>Weekly Test Report</h2>
<p>The report was generated successfully.</p>
"""

mail.Send()