🚨🩺 A new Nature Medicine paper co-authored by ARISE's own Eric Horvitz and colleagues rings the alarm bells on an overreliance on benchmarks for clinical AI evaluation, challenging that benchmark performance alone does not determine medical viability.

They conduct a series of pressure tests on leading AI models to evaluate performance under perturbations, which they show lead systems to guess the correct answer even with key inputs removed and get confused by the slightest prompt changes while constructing convincing but flawed reasoning traces.

In fact, the safest models in their study actually scored the worst.

For example, researchers took clinical cases that require an image to diagnose and then deleted the image. The right move from an AI tool would be to abstain in this situation. This is because you can't interpret a rash you can't see.

However, GPT-5 answered anyway, scoring 41% with no image (chance is 20%). It was leaning on disease prevalence and memorized pattern to arrive at the answer, not the picture as it should have.

GPT-4o scored just 16%, but only because it refused to guess half the time. If it removed those refusals, it matched other models at ~33%.

So the model that recognized it was missing critical information and declined to guess scored the worst on the benchmark leaderboard.

The takeaway for anyone deploying clinical AI: a benchmark score tells you a model can pass a test. It does not tell you whether the model is safe, or even that it's looking at the patient in question.

The authors recommend we stop treating performance on leaderboards as implied readiness and start stress-testing rigorously for the failure modes that actually hurt people.

What are your thoughts? Read the paper below for more.👇

#ClinicalAI #HealthcareAI #MedicalAI #DigitalHealth #LLMs #AIinMedicine #PatientSafety #AIEvaluation #HealthTech #Benchmarking
