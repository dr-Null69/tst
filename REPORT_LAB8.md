# ЗВІТ З ЛАБОРАТОРНОЇ РОБОТИ №8
**Тема:** Regression Testing and Flaky Test Debugging  
**Об'єкт тестування:** SUT B — Сервіс прокату обладнання (Варіант 12)  
**Середовище:** Python 3.14, pytest 8.4.2, PostgreSQL 17 (Docker / Testcontainers)

---

## 1. Мета роботи
Організувати regression test suite, виділити smoke subset за допомогою маркерів, виявити та дослідити навмисно закладені проблеми нестабільності (flaky tests), забезпечити повну ізоляцію тестів і підтвердити їхню стабільність при повторних запусках.

---

## 2. Організація Test Suite (Smoke та Regression)

У файлі `pyproject.toml` зареєстровано кастомний маркер `smoke`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "smoke: Критичні smoke тести (підмножина regression)"
]
```

### Розподіл тестів у `tests/test_rental_regression.py`:

* **Smoke subset (`@pytest.mark.smoke`)** — перевірка базового критичного функціоналу (3 тести):
  * `test_create_rental` — успішне створення активної оренди;
  * `test_return_rental` — успішне повернення оренди;
  * `test_rental_amount` — розрахунок вартості оренди.
* **Regression suite** — повний набір із 10 тестів (включає Smoke subset + граничні випадки, відмови бізнес-правил та асинхронні сценарії):
  * `test_equipment_stock` — збереження залишку обладнання на складі;
  * `test_equipment_rate` — збереження добового тарифу обладнання;
  * `test_rental_after_policy_check` — збереження оренди при затримці зовнішньої політики;
  * `test_rental_selection` — вибірка кількох оренд за ідентифікатором обладнання;
  * `test_stock_rejection` — відхилення замовлення при перевищенні наявного залишку;
  * `test_policy_rejection` — відхилення замовлення зовнішньою політикою;
  * `test_repeated_return` — заборона повторного повернення вже поверненої оренди.

---

## 3. Команди запуску

* **Запуск тільки Smoke subset:**
  ```powershell
  pytest tests/test_rental_regression.py -m smoke -v
  ```
  *Результат:* 3 passed, 7 deselected in ~8s.

* **Запуск повного Regression suite:**
  ```powershell
  pytest tests/test_rental_regression.py -v
  ```
  *Результат:* 10 passed in ~6.7s.

* **Ізольований запуск окремого тесту (перевірка незалежності):**
  ```powershell
  pytest tests/test_rental_regression.py -k test_return_rental -v
  ```
  *Результат:* 1 passed, 9 deselected in ~8.5s.

* **Перевірка стабільності (5 послідовних запусків):**
  ```powershell
  1..5 | ForEach-Object { Write-Host "--- Прогін $_ ---"; pytest tests/test_rental_regression.py -q }
  ```
  *Результат:* 5 з 5 прогонів успішні (10 passed, 0 failed).

---

## 4. Виявлені та виправлені Flaky Tests

### Зведена таблиця: Симптом → Причина → Виправлення

| # | Тип проблеми | Проблемний тест | Симптом | Першопричина (Root Cause) | Спосіб виправлення |
|:---:|---|---|---|---|---|
| **1** | **Order dependency** | `test_return_rental` | Падіння з помилкою `RentalRejectedError` (ID 0 не знайдено) при ізольованому запуску або зміні черги тестів. | Тест не створював власні дані, а читав глобальну змінну `rental_id`, яку мав проставити `test_create_rental`. | Прибрано глобальний стан. У `test_return_rental` додано локальне створення нової оренди перед її поверненням. |
| **2** | **Shared state / Dirty test data** | `test_equipment_stock` та `test_equipment_rate` | Другий тест падав з помилкою `UniqueViolation: Key (code)=(REGRESSION-EQUIPMENT) already exists`. | Обидва тести використовували однаковий код `"REGRESSION-EQUIPMENT"`. Колонка `code` в БД має обмеження `UNIQUE`, а база між тестами в модулі не очищалася. | Забезпечено унікальність коду кожного запису через `uuid4().hex[:16]` (`STK-...`, `RAT-...`) у межах ліміту `VARCHAR(40)`. |
| **3** | **Fixed wait / Timing dependency** | `test_rental_after_policy_check` | Падіння `assert len(saved) == 1 (0 == 1)` — запис у БД був відсутній. | Тест використовував фіксовану паузу `time.sleep(0.05)` (50 мс), тоді як фонова політика спала 200 мс. Перевірка відбувалася до збереження даних. | Замінено `time.sleep` на детерміноване condition-based очікування завершення фонового потоку: `operation.result(timeout=2.0)`. |
| **4** | **Unordered DB selection** | `test_rental_selection` | Падіння `assert actual == references` при зміні порядку або за наявності `REGRESSION_REVERSE_FIXTURE=true`. | У SQL-запиті був відсутній оператор `ORDER BY`. СКБД не гарантує порядок рядків вибірки без явного сортування. | Додано `ORDER BY r.reference` до SQL-запиту, а перевірку узгоджено з `sorted(references)`. |

---

## 5. Деталі виправлення коду (Before / After)

### 1) Order dependency: `test_return_rental`
* **Було (Flaky):**
  ```python
  rental_id = 0

  def test_create_rental(sut):
      global rental_id
      saved = sut[3].rent(...)
      rental_id = saved.id

  def test_return_rental(sut):
      returned = sut[3].return_rental(rental_id, 0) # Падає, якщо rental_id == 0
  ```
* **Стало (Стабільно):**
  ```python
  @pytest.mark.smoke
  def test_return_rental(sut):
      eq = item(sut)
      saved = sut[3].rent(request(eq)) # Створює власну оренду
      returned = sut[3].return_rental(saved.id, 0)
      assert returned.status == S.RETURNED
  ```

---

### 2) Shared state / Dirty test data: `test_equipment_stock` & `test_equipment_rate`
* **Було (Flaky):**
  ```python
  def test_equipment_stock(sut):
      saved = sut[1].create(Equipment(0, "REGRESSION-EQUIPMENT", 10000, 5))

  def test_equipment_rate(sut):
      # Помилка: duplicate key value violates unique constraint "equipment_code_key"
      saved = sut[1].create(Equipment(0, "REGRESSION-EQUIPMENT", 15000, 3))
  ```
* **Стало (Стабільно):**
  ```python
  def test_equipment_stock(sut):
      unique_code = f"STK-{uuid4().hex[:16]}"
      saved = sut[1].create(Equipment(0, unique_code, 10000, 5))

  def test_equipment_rate(sut):
      unique_code = f"RAT-{uuid4().hex[:16]}"
      saved = sut[1].create(Equipment(0, unique_code, 15000, 3))
  ```

---

### 3) Fixed wait: `test_rental_after_policy_check`
* **Було (Flaky):**
  ```python
  with ThreadPoolExecutor(max_workers=1) as executor:
      operation = executor.submit(rent)
      time.sleep(0.05) # Ненадійна фіксована пауза (політика триває 200 мс)
      saved = sut[2].find_rentals(equipment.id, S.ACTIVE)
      assert len(saved) == 1
  ```
* **Стало (Стабільно):**
  ```python
  with ThreadPoolExecutor(max_workers=1) as executor:
      operation = executor.submit(rent)
      # Детерміноване очікування результату задачі
      operation.result(timeout=2.0)
      saved = sut[2].find_rentals(equipment.id, S.ACTIVE)
      assert len(saved) == 1
  ```

---

### 4) Unordered SQL selection: `test_rental_selection`
* **Було (Flaky):**
  ```python
  actual = [row[0] for row in sut[0].execute(
      "SELECT r.reference FROM rentals r JOIN rental_items i ON i.rental_id=r.id WHERE i.equipment_id=%s", (equipment.id,))]
  assert actual == references
  ```
* **Стало (Стабільно):**
  ```python
  actual = [row[0] for row in sut[0].execute(
      "SELECT r.reference FROM rentals r JOIN rental_items i ON i.rental_id=r.id WHERE i.equipment_id=%s ORDER BY r.reference", (equipment.id,))]
  assert actual == sorted(references)
  ```

---

## 6. Контрольні питання до захисту

1. **Різниця між Smoke testing і Regression testing:**
   * *Smoke testing* — швидка перевірка критично важливих функцій системи для підтвердження її працездатності (підмножина regression).
   * *Regression testing* — глибока перевірка всієї системи для виявлення дефектів після внесення змін або багфіксів.
2. **Різниця між Failing test і Flaky test:**
   * *Failing test* — тест, який падає детерміновано через наявність реального дефекту в коді.
   * *Flaky test* — тест, результат якого (успіх/невдача) змінюється без внесення змін у код (через порядок запуску, спільний стан, таймінги тощо).
3. **Різниця між Fixed wait і Condition-based wait:**
   * *Fixed wait (`time.sleep`)* — безумовне очікування фіксованого часу, що веде до сповільнення або нестабільності тестів.
   * *Condition-based wait* — очікування настання конкретної події/зміни стану з граничним таймаутом.
4. **Різниця між Retry і реальним усуненням причини Flaky behavior:**
   * *Retry* лише приховує проблему та збільшує час виконання CI/CD.
   * *Реальне усунення* виправляє першопричину (забезпечує ізоляцію даних, прибирає спільний стан, додає синхронізацію).
