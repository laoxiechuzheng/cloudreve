package model

import (
	"errors"
	"regexp"
)

// linkKeyPattern 自定义链接标识（分享 /s/、外链 /f/ 之后的部分）允许的格式
var linkKeyPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// ErrLinkKeyInvalid 自定义链接格式不对，或是将来会分配给新对象的 HashID
var ErrLinkKeyInvalid = errors.New("invalid link key")

// LinkKeyTakenError 自定义链接已被另一个分享或外链使用
type LinkKeyTakenError struct {
	Name string // 占用者的名称（分享的源对象名、外链的文件名）
}

func (e LinkKeyTakenError) Error() string {
	return "link key is taken"
}
