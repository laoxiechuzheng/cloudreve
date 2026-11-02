package admin

import (
	"strings"

	model "github.com/cloudreve/Cloudreve/v3/models"
	"github.com/cloudreve/Cloudreve/v3/pkg/serializer"
	"github.com/gin-gonic/gin"
)

// ShareBatchService 分享批量操作服务
type ShareBatchService struct {
	ID []uint `json:"id" binding:"min=1"`
}

// Delete 删除文件
func (service *ShareBatchService) Delete(c *gin.Context) serializer.Response {
	if err := model.DB.Where("id in (?)", service.ID).Delete(&model.Share{}).Error; err != nil {
		return serializer.DBErr("Failed to delete share record", err)
	}
	return serializer.Response{}
}

// ShareKeyService 设置分享的自定义链接
type ShareKeyService struct {
	ID  uint   `json:"id" binding:"required"`
	Key string `json:"key" binding:"max=64"`
}

// Set 设置自定义链接，Key 为空时恢复为 HashID 链接
func (service *ShareKeyService) Set() serializer.Response {
	var share model.Share
	if err := model.DB.First(&share, service.ID).Error; err != nil {
		return serializer.Err(serializer.CodeShareLinkNotFound, "", err)
	}
	if err := share.SetCustomKey(service.Key); err != nil {
		return serializer.ShareKeyErr(err)
	}
	return serializer.Response{Data: share.Key()}
}

// Shares 列出分享
func (service *AdminListService) Shares() serializer.Response {
	var res []model.Share
	total := 0

	tx := model.DB.Model(&model.Share{})
	if service.OrderBy != "" {
		tx = tx.Order(service.OrderBy)
	}

	for k, v := range service.Conditions {
		tx = tx.Where(k+" = ?", v)
	}

	if len(service.Searches) > 0 {
		search := ""
		for k, v := range service.Searches {
			search += k + " like '%" + v + "%' OR "
		}
		search = strings.TrimSuffix(search, " OR ")
		tx = tx.Where(search)
	}

	// 计算总数用于分页
	tx.Count(&total)

	// 查询记录
	tx.Limit(service.PageSize).Offset((service.Page - 1) * service.PageSize).Find(&res)

	// 查询对应用户，同时计算HashID
	users := make(map[uint]model.User)
	hashIDs := make(map[uint]string, len(res))
	for _, file := range res {
		users[file.UserID] = model.User{}
		hashIDs[file.ID] = file.Key()
	}

	userIDs := make([]uint, 0, len(users))
	for k := range users {
		userIDs = append(userIDs, k)
	}

	var userList []model.User
	model.DB.Where("id in (?)", userIDs).Find(&userList)

	for _, v := range userList {
		users[v.ID] = v
	}

	return serializer.Response{Data: map[string]interface{}{
		"total": total,
		"items": res,
		"users": users,
		"ids":   hashIDs,
	}}
}
