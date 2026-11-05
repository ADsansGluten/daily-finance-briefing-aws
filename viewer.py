"""Lambda « viewer » : sert le dernier briefing via une Function URL.

Variable d'environnement : BUCKET (même bucket que la Lambda du briefing).
"""
import os

import boto3

s3 = boto3.client("s3")


def lambda_handler(event, context):
    try:
        obj = s3.get_object(Bucket=os.environ["BUCKET"], Key="briefings/latest.html")
        body, status = obj["Body"].read().decode("utf-8"), 200
    except s3.exceptions.NoSuchKey:
        body, status = "<p>Aucun briefing disponible pour le moment.</p>", 404
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "max-age=300"},
        "body": body,
    }
