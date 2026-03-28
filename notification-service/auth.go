package main

import (
	"errors"

	"github.com/golang-jwt/jwt/v5"
)

var jwtSecret []byte

func initAuth(secret string) {
	jwtSecret = []byte(secret)
}

// decodeToken validates a HS256 JWT and returns the user_id from the "sub" claim.
func decodeToken(tokenStr string) (string, error) {
	token, err := jwt.Parse(tokenStr, func(t *jwt.Token) (interface{}, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, errors.New("unexpected signing method")
		}
		return jwtSecret, nil
	})
	if err != nil {
		return "", err
	}
	claims, ok := token.Claims.(jwt.MapClaims)
	if !ok || !token.Valid {
		return "", errors.New("invalid token")
	}
	sub, ok := claims["sub"].(string)
	if !ok || sub == "" {
		return "", errors.New("missing sub claim")
	}
	return sub, nil
}
