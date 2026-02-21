import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, accuracy_score, confusion_matrix
import joblib
import warnings
warnings.filterwarnings('ignore')

print("🧠 Initializing AuraTrade AI Training Sequence...\n")

# 1. Load the Data
try:
    df = pd.read_csv("smc_training_data.csv")
    print(f"📦 Loaded Dataset: {len(df)} historical SMC setups.")
except FileNotFoundError:
    print("❌ ERROR: Cannot find 'smc_training_data.csv'. Make sure you run build_dataset.py first.")
    exit()

# 2. Prepare the Features (X) and Target (y)
# These are the variables the AI is allowed to look at to make its decision
features = [
    'type',             # 1 for Buy, 0 for Sell
    'fvg_size_pips',    # How big the gap was
    'rsi_at_entry',     # Was it overbought/oversold?
    'atr_at_entry',     # Was volatility high or low?
    'momentum_ratio',   # How aggressive was the displacement?
    'news_bias'         # Did the USD news support the trade?
]

# Drop any rows with missing data just to be safe
df = df.dropna(subset=features + ['LABEL_WIN'])

X = df[features]
y = df['LABEL_WIN'] # 1 = Win, 0 = Loss

# 3. Split the Data (80% for learning, 20% for testing the AI like a final exam)
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

print(f"📚 Training AI on {len(X_train)} setups...")
print(f"📝 Testing AI on {len(X_test)} unseen setups...\n")

# 4. Initialize and Train the Machine Learning Model
# We use class_weight='balanced' because there are more losses than wins in the data, 
# and we want the AI to pay extra attention to what makes a winning trade.
model = RandomForestClassifier(
    n_estimators=200,      # Number of "trees" in the forest
    max_depth=7,           # Maximum depth of logic to prevent overthinking
    min_samples_split=5,   # Minimum setups required to form a rule
    class_weight='balanced', 
    random_state=42
)

# 🔥 THIS IS WHERE THE AI ACTUALLY LEARNS
model.fit(X_train, y_train)

# 5. Test the AI's Accuracy
predictions = model.predict(X_test)
probabilities = model.predict_proba(X_test)[:, 1] # Get the % confidence for wins

print("==================================================")
print("📊 AI PERFORMANCE REPORT (UNSEEN TEST DATA)")
print("==================================================\n")

accuracy = accuracy_score(y_test, predictions)
print(f"🎯 Overall Accuracy: {accuracy * 100:.2f}%")

print("\n📈 Detailed Metrics:")
print(classification_report(y_test, predictions, target_names=["Loss (0)", "Win (1)"]))

# Feature Importance: Find out what the AI thinks is the most important factor
print("🧠 What the AI learned is most important:")
importances = model.feature_importances_
feature_importance = pd.DataFrame({'Feature': features, 'Importance': importances})
feature_importance = feature_importance.sort_values(by='Importance', ascending=False)
for index, row in feature_importance.iterrows():
    print(f"   - {row['Feature']}: {row['Importance'] * 100:.1f}%")

# 6. Save the Brain!
model_filename = "aura_model.pkl"
joblib.dump(model, model_filename)
print(f"\n💾 SUCCESS: AI Brain saved as '{model_filename}'")
print("This file can now be loaded into your live production server!")