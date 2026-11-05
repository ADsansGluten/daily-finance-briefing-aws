# Briefing finance quotidien - guide console AWS

Chaque matin en semaine, un e-mail résume l'actualité financière (flux RSS publics, résumé par Amazon Bedrock, sources citées). La dernière version est aussi consultable sur une page web.

```
EventBridge Scheduler (7h00, lun-ven)
        |
        v
  Lambda "daily-finance-briefing"
    1. lit les flux RSS (stdlib)
    2. résume avec Bedrock (Nova 2 Lite, profil EU)
    3. écrit la page dans S3 (briefings/)
    4. envoie l'e-mail via SES
                                   S3 --> Lambda "briefing-viewer" --> Function URL (page web)
Budgets + CloudWatch : coûts et logs
```

**Région à utiliser partout : Europe (Irlande) `eu-west-1`.** (Bedrock, SES et le profil d'inférence `eu.` y sont disponibles ; vérifie que la région affichée en haut à droite de la console reste la même à chaque étape.)

Temps total estimé : 10 à 15 h en apprenant la console. Les étapes 1 à 7 donnent un briefing fonctionnel.

---

## Étape 1 - Sécuriser le compte (30 min)
1. Connecte-toi en root, **active la MFA** (menu du compte > Security credentials > Assign MFA device).
2. **AWS Budgets** > Create budget > *Customize (advanced)* > Cost budget > mensuel, montant **10 USD**, alerte à 80 % vers ton e-mail. *(Activité « budget » du widget Explore AWS.)*
3. Pour travailler au quotidien, crée un utilisateur **IAM Identity Center** ou IAM avec droits admin et ne te sers plus du root. (Si tu bloques ici, garde le root pour aujourd'hui avec la MFA, et note-le comme dette à corriger.)

## Étape 2 - Bucket S3 (10 min)
S3 > Create bucket > nom unique, par exemple `briefing-finance-adrien-2026` > région eu-west-1 > **laisse « Block all public access » coché**. Note le nom exact.

## Étape 3 - Vérifier ton e-mail dans SES (10 min)
Amazon SES > Identities > Create identity > *Email address* > ton adresse > clique le lien reçu par e-mail. Le compte SES est en **sandbox** : expéditeur et destinataire doivent être vérifiés. Utilise la même adresse pour les deux.

## Étape 4 - Tester Bedrock dans le playground (10 min)
Bedrock > Playgrounds > Chat > Select model > Amazon > **EU Amazon Nova 2 Lite** > envoie « Résume en une phrase ce qu'est la volatilité ». *(Activité « Bedrock » du widget Explore AWS.)*
Ensuite : Bedrock > *Cross-region inference* (ou Inference profiles) : repère l'identifiant du profil **EU de Nova 2 Lite** (`eu.amazon.nova-2-lite-v1:0`). Si le nom diffère, tu le mettras dans la variable `MODEL_ID` à l'étape 5.

> **Compte neuf :** Bedrock peut répondre `AccessDeniedException ... account is currently being verified`. La vérification prend normalement moins de 2 heures. En attendant, teste la Lambda avec `{"no_ai": true}` (étape 6) : elle n'appelle pas Bedrock.

## Étape 5 - Lambda du briefing (1 h)
1. Lambda > Create function > *Author from scratch* > nom `daily-finance-briefing` > Runtime **Python 3.12** > *Create a new role with basic Lambda permissions*.
2. Onglet Code : remplace le contenu de `lambda_function.py` par le fichier fourni > **Deploy**.
3. Configuration > General configuration > Edit : **Timeout 1 min 30**, mémoire **256 Mo**.
4. Configuration > Environment variables : `BUCKET`, `SENDER`, `RECIPIENT`, et `MODEL_ID` si besoin.
5. Configuration > Permissions > clique le nom du rôle > Add permissions > Create inline policy > onglet JSON > colle `iam-policy.json` **en remplaçant le nom du bucket** > nom `briefing-policy`.

## Étape 6 - Premier test sans e-mail (20 min)
Onglet Test > événement `{"dry_run": true}` > Test. Pour tester sans Bedrock : `{"dry_run": true, "no_ai": true}`. Attendu : `"status": "ok"` et un nombre d'articles. Ouvre S3 > `briefings/latest.html` pour voir le résultat. En cas d'erreur : CloudWatch > Log groups > `/aws/lambda/daily-finance-briefing`.
Erreurs courantes :
- `AccessDeniedException` sur Bedrock : modèle non autorisé ou `MODEL_ID` incorrect (voir étape 4).
- `Flux ignoré` dans les logs : un flux RSS ne répond pas, ce n'est pas bloquant tant qu'il en reste un.

## Étape 7 - Vrai test avec e-mail (10 min)
Relance le test avec `{}`. Tu dois recevoir l'e-mail (regarde les spams).

## Étape 8 - Planification (30 min)
EventBridge > Scheduler > Create schedule > *Recurring* > *Cron-based* : `0 7 ? * MON-FRI *` > fuseau **Europe/Paris** > cible *AWS Lambda* > `daily-finance-briefing` > laisse créer le rôle d'exécution. Pour tester sans attendre, mets temporairement une heure dans 5 minutes.

## Étape 9 - Page web publique avec Function URL (45 min)
1. Lambda > Create function `briefing-viewer` > Python 3.12 > colle `viewer.py` > Deploy > variable `BUCKET`.
2. Ajoute au rôle de cette fonction une politique inline avec seulement `s3:GetObject` sur `arn:aws:s3:::TON-BUCKET/briefings/*`.
3. Configuration > Function URL > Create > Auth type **NONE** > Save. Ouvre l'URL : tu vois le dernier briefing. *(Activité « Lambda » du widget Explore AWS.)*
La page est publique : n'y mets rien de privé.

## Étape 10 - Vérifier les crédits et nettoyer
Billing > Credits : contrôle ce qui a été crédité. N'oublie pas de supprimer tout ce que tu crées pour les activités EC2 ou RDS.

---

## Coûts attendus
Un briefing = quelques milliers de tokens avec Nova Lite, soit des centimes par mois. Lambda, EventBridge et SES restent dans les offres gratuites à ce volume. Le seul risque est de laisser tourner autre chose (EC2, RDS).

## Pour aller plus loin (v2)
- Remplacer les politiques `Resource: "*"` (Bedrock, SES) par des ressources précises.
- Décrire toute l'infrastructure avec Terraform ou AWS SAM.
- Ajouter une alerte CloudWatch si la Lambda échoue.
- Brancher le résumé quotidien dans ton portfolio.
- Réutiliser les JSON de `briefings/` comme source pour le projet or.

## Post LinkedIn (à écrire à la fin)
Structure : le problème (trop d'actualités), l'architecture en une image, 3 choses apprises (IAM, sandbox SES, coût), le lien du dépôt GitHub, et ce que tu fais ensuite.
