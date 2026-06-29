# Codal Crawler — Django

کرالر مستقل گزارش‌های مالی سامانه کدال

## نصب و اجرا

### ۱. نصب پایتون (اگر ندارید)
از [python.org](https://python.org) نسخه 3.10+ نصب کنید.

### ۲. نصب پکیج‌ها
```bash
pip install -r requirements.txt
```

اگر pip مشکل داشت:
```bash
pip install -r requirements.txt --registry https://pypi.ir/simple
```

### ۳. ساخت دیتابیس
```bash
python manage.py migrate
```

### ۴. اجرا
```bash
python manage.py runserver
```

### ۵. باز کردن مرورگر
برو به: **http://localhost:8000**

---

## ویژگی‌ها

- ✅ مستقیم به codal.ir وصل می‌شه (بدون نیاز به پروکسی)
- ✅ کش دیتابیس (SQLite) — هر نماد فقط یه بار درخواست می‌شه
- ✅ فیلتر و مرتب‌سازی گزارش‌ها
- ✅ لینک مستقیم به کدال و دانلود اکسل
- ✅ رابط کاربری تاریک و حرفه‌ای
- ✅ ریسپانسیو (موبایل و دسکتاپ)
